"""在命令行点名采纳及 Skill 路由问题，与兼容评分标准共享调用。"""

import argparse
import json
from uuid import UUID, uuid4

from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_adoption import question_rubric
from weora_cli.knowledge_api import call
from weora_cli.knowledge_local_scoring import current_session, local_evidence, register_evidence
from weora_cli.knowledge_session_scoring import (
    archived_turns,
    grade_batch,
    grade_rubric,
    judge_call_id,
    rubric_groups,
    submit_label,
    with_isolation,
)
from weora_cli.knowledge_skill_capture import skill_capture

ADOPTION_NAME_PREFIX = "teamai:adoption:"
ROUTING_NAME_PREFIX = "teamai:routing:"
GENERATED_NAME_PREFIXES = (ADOPTION_NAME_PREFIX, ROUTING_NAME_PREFIX)
_SETTLE = "/session-scoring/named-calls"


def _label(pin: dict[str, JsonValue], verdict: dict[str, JsonValue]) -> dict[str, JsonValue]:
    payload = {"name": pin["name"], "label": verdict["label"], "driver": verdict["driver"]}
    if "routing" in pin:
        routing = as_object(pin["routing"])
        rubric = as_object(routing["rubric"])
        return payload | {
            "source_hash": pin["source_hash"], "evidence_hash": routing["evidence_hash"],
            "scorer_id": rubric.get("id"), "scorer_revision": rubric.get("revision"),
        }
    if "adoption" not in pin:
        return payload | {"scorer_id": pin["id"], "scorer_revision": pin["revision"]}
    document = as_object(pin["adoption"])
    rubric = as_object(document.get("rubric") or {})
    return payload | {
        "entry_id": document["id"], "entry_revision": document["revision"], "source_hash": pin["source_hash"],
        "scorer_id": rubric.get("id"), "scorer_revision": rubric.get("revision"),
    }


def _submit(client: Client, project: str | None, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        return call(client, project, _SETTLE, method="POST", payload=payload)
    except CollabError as error:
        if error.code != "WEORA_UNREACHABLE":
            raise
        # 回复丢失时重用同一组原标签和调用键。
        return call(client, project, _SETTLE, method="POST", payload=payload)


def routing_rubric(pin: dict[str, JsonValue]) -> dict[str, JsonValue]:
    question = as_object(pin["routing"])
    skill = as_object(question["skill"])
    evidence = {
        "skills": {
            "available": [{key: skill[key] for key in ("name", "namespace", "description")}],
            "used": question["used"],
        },
    }
    return as_object(question["rubric"]) | {
        "name": pin["name"],
        "evidence_files": [{"path": "evidence.json", "content": json.dumps(evidence, ensure_ascii=False)}],
    }


def local_skill_metadata(
    client: Client, project: str | None, metadata: dict[str, JsonValue], names: list[str],
) -> dict[str, JsonValue]:
    if not any(name.startswith(ROUTING_NAME_PREFIX) for name in names):
        return {}
    return skill_capture(
        client, project, str(metadata["runtime"]), str(metadata["subject_key"]), str(metadata["captured_at"]),
    )


def named_judge_groups(
    pins: list[dict[str, JsonValue]], *, batch: bool, isolation: JsonValue = None,
) -> list[list[dict[str, JsonValue]]]:
    rubrics = []
    for pin in pins:
        if "adoption" in pin:
            rubric = question_rubric(as_object(pin["adoption"]))
        elif "routing" in pin:
            rubric = routing_rubric(pin)
        else:
            rubric = pin
        rubrics.append(with_isolation(rubric, isolation))
    if batch:
        return rubric_groups(rubrics, batch=True)
    groups = [[row] for row in rubrics if not str(row["name"]).startswith(GENERATED_NAME_PREFIXES)]
    for family in GENERATED_NAME_PREFIXES:
        groups.extend(rubric_groups([row for row in rubrics if str(row["name"]).startswith(family)], batch=True))
    return groups


def submit_named_group(
    client: Client, project: str | None, subject: dict[str, JsonValue], call_id: UUID,
    pins: list[dict[str, JsonValue]], verdicts: dict[str, dict[str, JsonValue]], *, offer_mode: JsonValue = None,
    recommended_routing: bool = False,
) -> tuple[str, int, dict[str, dict[str, JsonValue]]]:
    payload: dict[str, JsonValue] = {
        "operation_id": str(call_id), "subject": subject,
        "items": [_label(pin, verdicts[str(pin["name"])]) for pin in pins],
    }
    if offer_mode is not None:
        payload["offer_mode"] = offer_mode
    if recommended_routing:
        payload["recommended_routing"] = True
    isolation = next((verdict["isolation"] for verdict in verdicts.values() if "isolation" in verdict), None)
    if isolation is not None:
        payload["isolation"] = isolation
    receipt = _submit(client, project, payload)
    returned = {str(row["scorer"]): row for row in items(receipt)}
    if set(returned) != {str(pin["name"]) for pin in pins}:
        raise CollabError("KNOWLEDGE_SCORER_PLAN", "评分收据未包含本次调用的全部判定。")
    identifier, revision = receipt.get("subject_id"), receipt.get("subject_revision")
    if not isinstance(identifier, str) or type(revision) is not int or revision < 1:
        raise CollabError("KNOWLEDGE_SCORER_PLAN", "评分收据缺少证据版本。")
    return identifier, revision, returned


def score_named(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    project, names, revision = args.project, args.scorer, args.revision
    if not 1 <= len(names) <= 20 or len(names) != len(set(names)):
        raise CollabError("KNOWLEDGE_SCORER_ARGUMENTS", "请指定 1 到 20 个不同的评分标准。")
    if args.label is not None and (len(names) != 1 or args.driver is not None):
        raise CollabError("KNOWLEDGE_SCORER_ARGUMENTS", "人工标签需要指定一个已发布评分标准。")
    local = args.archive is None
    metadata: dict[str, JsonValue] = {}
    turns: list[JsonValue] = []
    subject: dict[str, JsonValue]
    if local:
        if revision is not None:
            raise CollabError("KNOWLEDGE_SCORER_ARGUMENTS", "归档版本只用于 --archive。")
        runtime, native = (args.runtime, args.session) if args.session else current_session(args.runtime)
        metadata, turns = local_evidence(runtime, native, args.transcript, with_recall=True)
        subject = {"kind": "local", **metadata, **local_skill_metadata(client, project, metadata, names)}
    else:
        if revision is None or revision < 1 or args.runtime is not None or args.transcript is not None:
            raise CollabError("KNOWLEDGE_SCORER_ARGUMENTS", "请指定归档版本，宿主和转录路径只用于本机会话。")
        subject = {"kind": "archive", "archive_id": args.archive, "revision": revision}
    plan = call(client, project, "/session-scoring/named-plan", method="POST", payload={
        "subject": subject, "scorers": names, "accepts_isolation": True, "manual": args.label is not None,
    })
    pins = items(plan)
    if [row.get("name") for row in pins] != names:
        raise CollabError("KNOWLEDGE_SCORER_PLAN", "评分计划未包含所选标准及采纳问题。")
    if not local:
        archived = as_object(plan.get("archive") or {})
        if archived.get("id") != args.archive or archived.get("revision") != revision:
            raise CollabError("KNOWLEDGE_SCORER_PLAN", "评分计划未绑定所选归档。")
        digest = archived.get("digest")
        if not isinstance(digest, str):
            raise CollabError("KNOWLEDGE_SCORER_PLAN", "归档证据缺少摘要。")
        if args.label is None:
            turns = archived_turns(client, project, args.archive, revision, digest)
    by_name = {str(pin["name"]): pin for pin in pins}
    groups = named_judge_groups(pins, batch=args.batch or plan.get("batch") is True, isolation=plan.get("isolation"))
    operation = UUID(args.operation_id) if args.operation_id else uuid4()
    subject_id: str | None = None
    results: dict[str, JsonValue] = {}
    for group in groups:
        keys = [str(row["name"]) for row in group]
        call_id = judge_call_id(operation, group)
        try:
            verdicts = (
                {keys[0]: grade_rubric(group[0], turns, manual=args.label, driver=args.driver)} if len(group) == 1
                else grade_batch(group, turns, driver=args.driver)
            )
            if args.dry_run:
                results.update(verdicts)
            elif any(name.startswith(GENERATED_NAME_PREFIXES) for name in keys):
                settled_id, settled, returned = submit_named_group(
                    client, project, subject, call_id, [by_name[name] for name in keys], verdicts,
                )
                if local:
                    subject_id, revision = settled_id, settled
                results.update(returned)
            else:
                if local and subject_id is None:
                    metadata = {key: metadata[key] for key in ("runtime", "subject_key", "digest")}
                    subject_id, revision = register_evidence(client, project, metadata)
                identifier = subject_id if local else args.archive
                assert identifier is not None and revision is not None
                for name in keys:
                    results[name] = submit_label(
                        client, project, identifier, revision, by_name[name], verdicts[name], operation,
                        judge_call=call_id if len(group) > 1 else None,
                    )
        except CollabError as error:
            for name in keys:
                results[name] = {"scorer": name, "error": error.code, "message": str(error)}
    target = {"subject_id": subject_id, "subject_revision": revision, "evidence": "local"} if local else {
        "archive_id": args.archive, "archive_revision": revision,
    }
    return target | {
        "operation_id": str(operation), "preview": args.dry_run, "items": [results[name] for name in names],
    }
