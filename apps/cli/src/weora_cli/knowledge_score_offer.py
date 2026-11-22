"""把评分建议和脱敏快照绑定到一次可确认或跳过的本机预览。"""

import argparse
import hashlib
import json
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from weora_cli.collab_client import Client, CollabError, as_object, items, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_judge_isolation import judge_isolation
from weora_cli.knowledge_local_scoring import current_session, evidence_digest, local_evidence, register_evidence
from weora_cli.knowledge_named_scoring import (
    GENERATED_NAME_PREFIXES,
    ROUTING_NAME_PREFIX,
    local_skill_metadata,
    named_judge_groups,
    submit_named_group,
)
from weora_cli.knowledge_session_scoring import (
    archived_turns,
    grade_batch,
    grade_rubric,
    judge_call_id,
    rubric_groups,
    submit_label,
    with_isolation,
)
from weora_cli.knowledge_settings import session_state_root
from weora_cli.knowledge_skill_capture import skill_capture

_API = "/session-scoring/offer"
_VALID_FOR = 24 * 3600
_CODE = re.compile(r"[0-9a-f]{64}")


def _fingerprint(device: str, offer: dict[str, JsonValue]) -> str:
    encoded = json.dumps([device, offer], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _choices(rubrics: list[dict[str, JsonValue]]) -> list[JsonValue]:
    selected: list[JsonValue] = []
    for row in rubrics:
        if "routing" in row:
            question = as_object(row["routing"])
            skill = as_object(question["skill"])
            rubric = as_object(question["rubric"])
            selected.append({
                "name": row["name"], "source_hash": row["source_hash"], "evidence_hash": question["evidence_hash"],
                "skill": {key: skill[key] for key in ("name", "namespace", "version_id")},
                "id": rubric.get("id"), "revision": rubric.get("revision"),
                "tool": rubric.get("tool"), "model": rubric.get("model"),
                **({"isolation": rubric["isolation"]} if "isolation" in rubric else {}),
            })
        elif "adoption" in row:
            entry = as_object(row["adoption"])
            rubric = as_object(entry.get("rubric") or {})
            selected.append({
                "name": row["name"], "entry_id": entry["id"], "entry_revision": entry["revision"],
                "source_hash": row["source_hash"], "id": rubric.get("id"), "revision": rubric.get("revision"),
                "tool": rubric.get("tool"), "model": rubric.get("model"),
                **({"isolation": rubric["isolation"]} if "isolation" in rubric else {}),
            })
        else:
            selected.append({
                "id": row["id"], "name": row["name"], "revision": row["revision"],
                "sampling_rate": row.get("sampling_rate"), "tool": row.get("tool"), "model": row.get("model"),
                **({"source_hash": row["source_hash"]} if "source_hash" in row else {}),
                **({"isolation": row["isolation"]} if "isolation" in row else {}),
            })
    return selected


def _prepare(client: Client, args: argparse.Namespace, root: Path, device: str) -> dict[str, JsonValue]:
    turns: list[JsonValue] = []
    subject: dict[str, JsonValue]
    digest: str
    if args.archive is not None:
        if not isinstance(args.revision, int) or args.revision < 1 or args.runtime or args.transcript:
            raise CollabError("KNOWLEDGE_SCORE_OFFER_ARGUMENTS", "归档预览需要 --archive 和 --revision。")
        metadata = call(client, args.project, f"/shared-sessions/{args.archive}", revision=args.revision)
        resolved = metadata.get("digest")
        if (
            metadata.get("id") != args.archive or metadata.get("revision") != args.revision
            or not isinstance(resolved, str)
        ):
            raise CollabError("KNOWLEDGE_SCORE_OFFER_CHANGED", "归档版本与预览指定范围不一致。")
        title, count, digest = metadata.get("title"), metadata.get("turn_count"), resolved
        subject = {"kind": "archive", "archive_id": args.archive, "revision": args.revision}
    else:
        if args.revision is not None:
            raise CollabError("KNOWLEDGE_SCORE_OFFER_ARGUMENTS", "--revision 只用于已归档会话。")
        runtime, session = (args.runtime, args.session) if args.session else current_session(args.runtime)
        with_recall = any(name.startswith(GENERATED_NAME_PREFIXES) for name in args.scorer)
        metadata, turns = local_evidence(runtime, session, args.transcript, with_recall=with_recall)
        delivery = local_skill_metadata(client, args.project, metadata, args.scorer)
        if not args.scorer:
            captured = datetime.now(UTC).isoformat()
            delivery = skill_capture(
                client, args.project, str(metadata["runtime"]), str(metadata["subject_key"]), captured,
            )
            if delivery:
                metadata = metadata | {
                    "recall_session_key": hashlib.sha256(str(UUID(session)).encode()).hexdigest(),
                    "captured_at": captured,
                }
        subject = {"kind": "local", **metadata, **delivery}
        digest = str(metadata["digest"])
        title, count = "本机会话", len(turns)

    plan = call(client, args.project, _API, method="POST", payload={
        "run_mode": args.run_mode or "attended", "subject": subject, "scorers": args.scorer, "accepts_isolation": True,
        "accept_routing": True,
    })
    selected = _choices(items(plan))
    batch = args.batch or plan.get("batch") is True
    preview: dict[str, JsonValue] = {
        "status": plan["status"], "title": title, "turn_count": count, "evidence": subject["kind"],
        "project_id": args.project, "items": selected, "confirm": None,
    }
    if not selected:
        return preview
    if batch:
        preview["batch"] = True
    isolation = judge_isolation(plan.get("isolation"))
    if isolation is not None:
        preview["isolation"] = isolation
    now = time.time()
    for path in root.glob("*.json"):
        with suppress(OSError):
            if _CODE.fullmatch(path.stem) and path.stat().st_mtime < now - _VALID_FOR:
                path.unlink()
    offer: dict[str, JsonValue] = {
        "project": args.project, "mode": args.run_mode or "attended", "created_at": now, "operation": str(uuid4()),
        "subject": subject, "digest": digest, "selected": selected, "batch": batch,
    }
    if isolation is not None:
        offer["isolation"] = isolation
    if not args.scorer and any("routing" in item for item in items(plan)):
        offer["recommended_routing"] = True
    token = _fingerprint(device, offer)
    write_json(root / f"{token}.json", {"offer": offer, "turns": turns})
    return preview | {"confirm": token}


def _checked(
    path: Path, token: str, device: str, project: str | None,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    try:
        record = as_object(json.loads(path.read_text(encoding="utf-8")))
        offer = as_object(record.get("offer"))
        created = offer.get("created_at")
        if (
            offer.get("project") == project and _fingerprint(device, offer) == token
            and isinstance(created, int | float) and 0 <= time.time() - created <= _VALID_FOR
        ):
            return record, offer
    except (OSError, ValueError, RecursionError, CollabError):
        pass
    raise CollabError("KNOWLEDGE_SCORE_OFFER_CHANGED", "预览已过期、不可读取或范围不一致，请重新预览。")


def _accept(
    client: Client, project: str | None, path: Path, record: dict[str, JsonValue], offer: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    selected = items({"items": offer.get("selected")})
    subject = as_object(offer.get("subject"))
    request: dict[str, JsonValue] = {
        "run_mode": offer["mode"], "subject": subject, "scorers": [row["name"] for row in selected],
        "accepts_isolation": True,
    }
    recommended = offer.get("recommended_routing") is True
    if recommended:
        request["recommended_routing"] = True
    plan = call(client, project, _API, method="POST", payload=request)
    rubrics = items(plan)
    isolation = judge_isolation(plan.get("isolation"))
    if (
        plan.get("status") not in ("ask", "auto") or _choices(rubrics) != selected
        or isolation != offer.get("isolation")
    ):
        raise CollabError("KNOWLEDGE_SCORE_OFFER_CHANGED", "评分标准或策略已变化，请重新预览。")

    native = subject.get("kind") == "local"
    identifier, revision = str(subject.get("archive_id")), int(str(subject.get("revision", 0)))
    if native:
        local_turns = record.get("turns")
        if (
            not isinstance(local_turns, list)
            or evidence_digest(str(subject["subject_key"]), local_turns) != offer["digest"]
        ):
            raise CollabError("KNOWLEDGE_SCORE_OFFER_CHANGED", "脱敏快照与核对的指纹不一致。")
        turns = local_turns
    else:
        turns = archived_turns(client, project, identifier, revision, offer["digest"])

    verdicts = as_object(record.setdefault("verdicts", {}))
    receipts = as_object(record.setdefault("receipts", {}))
    operation = UUID(str(offer["operation"]))
    generated = any("adoption" in row or "routing" in row for row in rubrics)
    pins = {str(row["name"]): row for row in rubrics}
    batch = offer.get("batch") is True
    groups = (
        named_judge_groups(rubrics, batch=batch, isolation=isolation) if generated
        else rubric_groups([with_isolation(rubric, isolation) for rubric in rubrics], batch=batch)
    )
    outcome: dict[str, JsonValue] = {}
    for group in groups:
        names = [str(row["name"]) for row in group]
        group_call = judge_call_id(operation, group) if len(group) > 1 else None
        extras = {"judge_call": group_call} if group_call is not None else {}
        if any(name not in verdicts and name not in receipts for name in names):
            try:
                if group_call is None:
                    verdicts[names[0]] = grade_rubric(group[0], turns)
                else:
                    verdicts.update(grade_batch(group, turns))
                # 整组先保存判定，再写标签。提交失败的重试使用同一次模型结果。
                write_json(path, record)
            except CollabError as error:
                for name in names:
                    outcome[name] = {"scorer": name, "error": error.code, "message": str(error)}
                continue
        if generated:
            if all(name in receipts for name in names):
                outcome.update({name: receipts[name] for name in names})
                continue
            try:
                options = {"recommended_routing": True} if recommended and any(
                    name.startswith(ROUTING_NAME_PREFIX) for name in names
                ) else {}
                _, _, saved = submit_named_group(
                    client, project, subject, group_call or judge_call_id(operation, group),
                    [pins[name] for name in names], {name: as_object(verdicts[name]) for name in names},
                    offer_mode=offer["mode"], **options,
                )
                receipts.update(saved)
                outcome.update(saved)
                write_json(path, record)
            except CollabError as error:
                for name in names:
                    outcome[name] = {"scorer": name, "error": error.code, "message": str(error)}
            continue
        for rubric, name in zip(group, names, strict=True):
            if name in receipts:
                outcome[name] = receipts[name]
                continue
            try:
                if native and revision == 0:
                    metadata = {field: subject[field] for field in ("runtime", "subject_key", "digest")}
                    identifier, revision = register_evidence(client, project, metadata)
                receipt = submit_label(
                    client, project, identifier, revision, rubric, as_object(verdicts[name]), operation, **extras,
                )
                receipts[name] = receipt
                write_json(path, record)
                outcome[name] = receipt
            except CollabError as error:
                outcome[name] = {"scorer": name, "error": error.code, "message": str(error)}
    completed = len(receipts) == len(rubrics)
    result: dict[str, JsonValue] = {
        "status": "completed" if completed else "pending", "items": [outcome[str(row["name"])] for row in rubrics],
    }
    if completed:
        write_json(path, {"offer": offer, "result": result})
    return result


def score_offer(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    device = session_state_root(client)
    root = device / "scoring-offers"
    token = args.confirm or args.skip
    if token is None:
        return _prepare(client, args, root, device.name)
    selection = (args.archive, args.revision, args.runtime, args.session, args.transcript)
    if (
        not _CODE.fullmatch(token) or args.scorer or args.run_mode or args.batch
        or any(value is not None for value in selection)
    ):
        raise CollabError("KNOWLEDGE_SCORE_OFFER_ARGUMENTS", "确认或跳过时只提交预览码和原项目范围。")
    path = root / f"{token}.json"
    with lock_file(path):
        record, offer = _checked(path, token, device.name, args.project)
        if "result" in record:
            return as_object(record["result"])
        if args.skip is not None:
            receipts = as_object(record.get("receipts", {}))
            result: dict[str, JsonValue] = {"status": "skipped", "items": list(receipts.values())}
            write_json(path, {"offer": offer, "result": result})
            return result
        return _accept(client, args.project, path, record, offer)
