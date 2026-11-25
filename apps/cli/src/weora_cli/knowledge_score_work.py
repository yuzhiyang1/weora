"""从当前项目领取后台评分，按证据版本读取本机快照或归档。"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

from ruamel.yaml.error import YAMLError

from weora_cli.collab_client import Client, CollabError, as_object, items, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_adoption import question_rubric, score_candidate, score_candidate_batch
from weora_cli.knowledge_api import call
from weora_cli.knowledge_judge_isolation import isolation_level, judge_isolation, verified_clients
from weora_cli.knowledge_markdown import parse_metadata
from weora_cli.knowledge_named_scoring import routing_rubric
from weora_cli.knowledge_score_evidence_cache import (
    DeviceEvidence,
    EvidenceKey,
    ProjectForDirectory,
    device_evidence,
    freeze_ready,
    note_finished,
    note_resumed,
)
from weora_cli.knowledge_session_scoring import archived_turns, grade_batch, grade_rubric, judge_session, with_isolation
from weora_cli.knowledge_settings import session_state_root

_POLICY = "/settings/session-scoring"
_BASE = "/session-scoring"
_DIRECTORY = re.compile(r"\w(?:[\w.-]{0,62}[\w-])?")
_POLICY_FILE_LIMIT = 16 * 1024


def _preferences(client: Client) -> Path:
    return session_state_root(client) / "scoring-settings.json"


def _read(path: Path) -> dict[str, JsonValue]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}


def _binding(client: Client, cwd: Path, mode: str) -> tuple[str, str | None]:
    config = _read(_preferences(client))
    current = cwd.resolve()
    for parent in (current, *current.parents):
        values = config.get(str(parent))
        if not isinstance(values, dict) or mode not in values:
            continue
        entry = values[mode]
        if isinstance(entry, str):
            return "auto", str(UUID(entry))
        if isinstance(entry, dict) and entry.get("decision") in ("ask", "auto"):
            decision, project = entry.get("decision"), entry.get("project")
            if isinstance(decision, str) and isinstance(project, str):
                return decision, str(UUID(project))
        return "off", None
    return "off", None


def _bound_project(client: Client, cwd: Path, mode: str) -> str | None:
    decision, project = _binding(client, cwd, mode)
    return project if decision == "auto" else None


def _directory_payload(path: Path) -> dict[str, JsonValue]:
    try:
        with path.open("rb") as handle:
            data = handle.read(_POLICY_FILE_LIMIT + 1)
        if len(data) > _POLICY_FILE_LIMIT:
            raise ValueError("文件超过 16 KiB。")
        settings = parse_metadata(data.decode("utf-8-sig"), rule=False)
    except (OSError, ValueError, YAMLError, RecursionError) as exc:
        raise CollabError("KNOWLEDGE_SCORING_SETTINGS_FILE", "无法读取目录评分 YAML，请检查格式和文件大小。") from exc
    if settings is None:
        return {}
    supported = {"sessions", "daily_limit", "batch", "recall_adoption", "isolation", "skill_routing"}
    if not isinstance(settings, dict) or settings.keys() - supported:
        known = "、".join(sorted(supported))
        raise CollabError("KNOWLEDGE_SCORING_SETTINGS_FILE", f"评分 YAML 只接受以下字段。{known}。")
    sessions = settings.get("sessions", {})
    if not isinstance(sessions, bool) and (
        not isinstance(sessions, dict) or sessions.keys() - {"attended", "unattended"}
        or any(mode not in ("off", "ask", "auto") for mode in sessions.values())
    ):
        raise CollabError("KNOWLEDGE_SCORING_SETTINGS_FILE", "sessions 应为布尔值或有效的运行模式配置。")
    limit = settings.get("daily_limit")
    if limit is not None and (type(limit) is not int or not 0 <= limit <= 100):
        raise CollabError("KNOWLEDGE_SCORING_SETTINGS_FILE", "daily_limit 应为 0 到 100 的整数。")
    batch = settings.get("batch")
    if batch is not None and not isinstance(batch, bool):
        raise CollabError("KNOWLEDGE_SCORING_SETTINGS_FILE", "batch 应为布尔值。")
    payload: dict[str, JsonValue] = {"sessions": sessions, "daily_limit": limit, "batch": batch}
    for setting in ("recall_adoption", "skill_routing"):
        if setting in settings:
            selected = settings[setting]
            if selected is not None and not isinstance(selected, bool):
                raise CollabError("KNOWLEDGE_SCORING_SETTINGS_FILE", f"{setting} 应为布尔值。")
            payload[setting] = selected
    if "isolation" in settings:
        raw = settings["isolation"]
        if raw is not None and (not isinstance(raw, str) or not raw.strip()):
            raise CollabError("KNOWLEDGE_SCORING_SETTINGS_FILE", "isolation 应为 best-effort 或 verified。")
        payload["isolation"] = isolation_level(raw)
    return payload


def _configure_directory(
    client: Client, args: argparse.Namespace, namespace: str | None,
) -> dict[str, JsonValue]:
    if (not namespace or not _DIRECTORY.fullmatch(namespace) or args.project or args.workspace
            or args.daily_limit is not None or args.batch is not None
            or getattr(args, "isolation", None) is not None or getattr(args, "skill_routing", None) is not None):
        raise CollabError("KNOWLEDGE_SCORING_ARGUMENTS", "请用 --namespace 指定要查看或导入的评分目录。")
    suffix = f"{_POLICY}/namespaces/{quote(namespace, safe='')}"
    path, revision = args.file, args.base_revision
    if args.decision == "status" and path is None and revision is None:
        return call(client, None, suffix)
    if args.decision != "apply" or path is None or revision is None or revision < 0:
        raise CollabError("KNOWLEDGE_SCORING_ARGUMENTS", "导入评分目录需要 apply、--file 和 --base-revision。")
    payload = _directory_payload(path) | {"base_revision": revision}
    return call(client, None, suffix, method="PUT", payload=payload)


def configure(client: Client, cwd: Path, args: argparse.Namespace) -> dict[str, JsonValue]:
    decision = args.decision
    namespace = getattr(args, "namespace", None)
    if namespace is not None or decision == "apply":
        return _configure_directory(client, args, namespace)
    if getattr(args, "file", None) is not None or getattr(args, "base_revision", None) is not None:
        raise CollabError("KNOWLEDGE_SCORING_ARGUMENTS", "--file 和 --base-revision 需要用于评分目录导入。")
    isolation = getattr(args, "isolation", None)
    routing = getattr(args, "skill_routing", None)
    if args.workspace:
        editing = any(value is not None for value in (args.daily_limit, args.batch, isolation, routing))
        if args.project is not None or decision == "inherit" or (decision == "status" and editing):
            raise CollabError("KNOWLEDGE_SCORING_ARGUMENTS", "工作空间提供 status、off、ask 和 auto。")
        if decision == "status":
            return as_object(call(client, None, _POLICY, workspace_defaults=1).get(args.run_mode))
        payload: dict[str, JsonValue] = {"action": decision, "daily_limit": args.daily_limit}
        if args.batch is not None:
            payload["batch"] = args.batch
        if isolation is not None:
            payload["isolation"] = isolation
        if routing is not None:
            payload["skill_routing"] = routing
        return call(client, None, f"{_POLICY}/{args.run_mode}", method="PUT", payload=payload)
    if isolation is not None or routing is not None:
        raise CollabError("KNOWLEDGE_SCORING_ARGUMENTS", "--isolation 和 --skill-routing 用于设置工作空间策略。")
    if decision == "status" and args.project and args.daily_limit is None and args.batch is None:
        return as_object(call(client, args.project, _POLICY).get(args.run_mode))
    needs_project = decision in ("ask", "auto")
    if args.daily_limit is not None or args.batch is not None or needs_project != (args.project is not None):
        raise CollabError("KNOWLEDGE_SCORING_ARGUMENTS", "目录可查询、关闭、继承，或指定项目启用询问或自动评分。")
    if decision != "status":
        if args.project is not None:
            call(client, args.project, query="", limit=1)
            policy = as_object(call(client, args.project, _POLICY).get(args.run_mode))
            allowed = ("ask", "auto") if decision == "ask" else ("auto",)
            if policy.get("action") not in allowed or policy.get("daily_limit") == 0:
                raise CollabError("KNOWLEDGE_SCORING_DISABLED", "工作空间当前未允许所选运行模式和评分策略。")
        path, key = _preferences(client), str(cwd.resolve())
        with lock_file(path):
            config = _read(path)
            selected = config.get(key)
            values = selected if isinstance(selected, dict) else {}
            if decision == "inherit":
                values.pop(args.run_mode, None)
            elif decision in ("ask", "auto"):
                values[args.run_mode] = {"decision": decision, "project": args.project}
            else:
                values[args.run_mode] = None
            write_json(path, config | {key: values})
    chosen, project = _binding(client, cwd, args.run_mode)
    if os.environ.get("WEORA_SCORING_DISABLED") == "1":
        return {"action": "off", "source": "environment", "project_id": project}
    return {"action": chosen, "project_id": project}


def score_offer_hint(client: Client, cwd: Path, runtime: str, session: str) -> str:
    """只在经验分享提醒里补充询问入口，宿主需先向用户展示预览。"""
    if runtime not in ("codex", "claude-code") or os.environ.get("WEORA_SCORING_DISABLED") == "1":
        return ""
    decision, project = _binding(client, cwd, "attended")
    if project is None:
        return ""
    policy = as_object(call(client, project, _POLICY).get("attended"))
    if policy.get("daily_limit") == 0 or policy.get("action") not in ("ask", "auto"):
        return ""
    if decision != "ask" and policy.get("action") != "ask":
        return ""
    return (f" 可用 weora context score-offer --project {project} --runtime {runtime} --session {UUID(session)} "
            "查看团队建议的评分标准。请先展示预览，请用户选择或跳过。"
            "用户点名采纳问题时，用 --scorer 选择后重新预览。收到同意后再确认。")


def start_if_bound(client: Client, cwd: Path, mode: str) -> None:
    """启动 Hook 与结束归档可触发同一后台入口，领取由服务端去重。"""
    if os.environ.get("WEORA_SCORING_DISABLED") == "1":
        return
    project = _bound_project(client, cwd, mode)
    if project is not None:
        command = [sys.executable, "-m", "weora_cli", "context", "score-drain", "--run-mode", mode,
                   "--project", project, "--bound"]
        subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                         start_new_session=os.name != "nt")


def observe_scoring(client: Client, cwd: Path, runtime: str, event: Mapping[str, JsonValue]) -> None:
    """收到真正的根会话事件后，维护与归档开关无关的本机候选。"""
    if os.environ.get("WEORA_SCORING_DISABLED") == "1":
        return
    name, session, transcript = event.get("hook_event_name"), event.get("session_id"), event.get("transcript_path")
    if not isinstance(session, str):
        return
    if name in ("SessionStart", "UserPromptSubmit"):
        note_resumed(client, runtime, session)
    elif name == "Stop" and isinstance(transcript, str):
        project = _bound_project(client, cwd, "attended")
        if project is not None:
            note_finished(client, project, cwd, "attended", runtime, session, Path(transcript))


def stage_drained_session(
    client: Client, cwd: Path, runtime: str, session: str, transcript: Path | None, *,
    stopped_at: float | None = None, expected_project: str | None = None,
) -> None:
    """由无人值守启动器登记已退出的进程，不等待交互闲置门槛。"""
    project = None if os.environ.get("WEORA_SCORING_DISABLED") == "1" else _bound_project(client, cwd, "unattended")
    if expected_project is not None and project != expected_project:
        raise CollabError("KNOWLEDGE_DRAIN_BINDING_CHANGED", "目录的评分项目已变化。")
    if project is not None:
        note_finished(client, project, cwd, "unattended", runtime, session, transcript, stopped_at=stopped_at)


def _post(client: Client, project: str | None, path: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        return call(client, project, path, method="POST", payload=payload)
    except CollabError as error:
        if error.code != "WEORA_UNREACHABLE":
            raise
        # 领取键与判定都保持原值，即使前一个回复丢失也能取回收据。
        return call(client, project, path, method="POST", payload=payload)


def _batch_rubric(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if "routing" in row:
        return routing_rubric(row)
    if "adoption" in row:
        return question_rubric(as_object(row["adoption"]))
    return as_object(row.get("scorer"))


def _attempt_outcome(
    client: Client, project: str | None, work: dict[str, JsonValue], enabled: Callable[[], bool],
    device: dict[EvidenceKey, DeviceEvidence], project_for: ProjectForDirectory,
) -> dict[str, JsonValue]:
    archive = as_object(work.get("archive"))
    identifier, revision, digest = archive.get("id"), archive.get("revision"), archive.get("digest")
    if not isinstance(identifier, str) or type(revision) is not int or not isinstance(digest, str):
        return {"error": "evidence_unavailable"}
    evidence = device.get((identifier, revision, digest)) if archive.get("evidence") == "local" else None

    def allowed() -> bool:
        return enabled() and (evidence is None or project_for(evidence.cwd) == project)

    try:
        if archive.get("evidence") == "local":
            if evidence is None or not allowed():
                return {"error": "evidence_unavailable"}
            turns = evidence.load()
        else:
            turns = archived_turns(client, project, identifier, revision, digest)
    except CollabError:
        return {"error": "evidence_unavailable"}
    if not allowed():
        return {"error": "evidence_unavailable"}
    try:
        outcome: dict[str, JsonValue]
        isolation = judge_isolation(work.get("isolation"))
        if work.get("routing_batch") is not None:
            rubrics = [
                with_isolation(routing_rubric(pin), isolation) for pin in items({"items": work["routing_batch"]})
            ]
            if not rubrics:
                raise CollabError("KNOWLEDGE_SCORER_LABEL", "服务未返回任何 Skill 路由问题。")
            first = rubrics[0]
            verdicts = (
                {str(first["name"]): grade_rubric(first, turns)} if len(rubrics) == 1 else grade_batch(rubrics, turns)
            )
            isolation = judge_isolation(isolation, *(rubric.get("isolation") for rubric in rubrics))
            outcome = {"labels": {name: value["label"] for name, value in verdicts.items()}}
        elif work.get("adoption") is not None:
            adoption = as_object(work["adoption"])
            rubric = as_object(adoption.get("rubric") or {})
            isolation = judge_isolation(isolation, rubric.get("isolation"))
            options = {"isolation": isolation} if isolation is not None else {}
            outcome = {"label": score_candidate(turns, adoption, **options)}
        elif work.get("adoption_batch") is not None:
            members = items({"items": work["adoption_batch"]})
            questions = [as_object(row.get("adoption")) for row in members]
            isolation = judge_isolation(
                isolation, *(as_object(row.get("rubric") or {}).get("isolation") for row in questions),
            )
            options = {"isolation": isolation} if isolation is not None else {}
            batch_labels = score_candidate_batch(turns, questions, **options)
            outcome = {"labels": {
                str(row["attempt_id"]): label for row, label in zip(members, batch_labels, strict=True)
            }}
        elif work.get("batch") is not None or work.get("mixed_batch") is not None:
            members = items({"items": work.get("mixed_batch", work.get("batch"))})
            # 普通、采纳和路由问题共用转录，各自保留原标准和证据。
            rubrics = [with_isolation(_batch_rubric(row), isolation) for row in members]
            if len(rubrics) < 2:
                raise CollabError("KNOWLEDGE_SCORER_LABEL", "服务返回的合并评分组不完整。")
            result = grade_batch(rubrics, turns)
            isolation = judge_isolation(isolation, *(rubric.get("isolation") for rubric in rubrics))
            outcome = {"labels": {
                str(row["attempt_id"]): result[str(rubric["name"])]["label"]
                for row, rubric in zip(members, rubrics, strict=True)
            }}
        else:
            rubric = with_isolation(as_object(work.get("scorer")), isolation)
            isolation = judge_isolation(rubric.get("isolation"))
            driver = "claude" if rubric.get("tool") == "claude" else "codex"
            labels = [str(row["value"]) for row in items({"items": rubric.get("labels")})]
            outcome = {"label": judge_session(driver, rubric, turns, labels)}
        if isolation is not None:
            outcome["isolation"] = isolation
    except CollabError as error:
        invalid = error.code in ("KNOWLEDGE_SCORER_LABEL", "KNOWLEDGE_ADOPTION_VERDICT_INVALID")
        return {"error": "invalid_answer" if invalid else "judge_failed"}
    return outcome if allowed() else {"error": "evidence_unavailable"}


def drain(client: Client, cwd: Path, args: argparse.Namespace) -> dict[str, JsonValue]:
    if not 1 <= args.limit <= 20:
        raise CollabError("INVALID_LIMIT", "后台评分一次可领取 1 至 20 项。")
    project, mode = args.project, args.run_mode

    def project_for(directory: Path) -> str | None:
        return _bound_project(client, directory, mode)

    def enabled() -> bool:
        if os.environ.get("WEORA_SCORING_DISABLED") == "1":
            return False
        return not args.bound or (project is not None and project_for(cwd) == project)

    completed: list[JsonValue] = []
    status = "off"
    # 单次模型请求最多 10 分钟，后台轮次在 20 分钟后停止领取新工作。
    stop = time.monotonic() + 1200
    guard = session_state_root(client) / "scoring-runs" / f"{project}-{mode}.json"
    if not enabled():
        return {"status": status, "items": completed}
    verified_tools = verified_clients()
    local_verified = judge_isolation() == "verified"
    if local_verified and not verified_tools:
        return {"status": "isolation_unavailable", "items": completed}
    with lock_file(guard):
        device: dict[EvidenceKey, DeviceEvidence] = {}
        if project is not None:
            freeze_ready(client, project, mode, project_for)
            device = device_evidence(client, project, mode, project_for)
        references = [entry.reference() for entry in device.values()]
        batches = [references[index:index + 100] for index in range(0, len(references), 100)] or [[]]
        batch = 0
        for _ in range(args.limit):
            if not enabled() or time.monotonic() >= stop:
                break
            while True:
                operation = str(uuid4())
                request: dict[str, JsonValue] = {
                    "operation_id": operation, "run_mode": mode, "accept_batch": True, "accept_adoption": True,
                    "accept_adoption_template": True,
                    "accept_adoption_batch": True,
                    "accept_mixed_batch": True, "accepts_isolation": True, "verified_tools": list(verified_tools),
                    "local_verified": local_verified, "accept_routing": True, "accept_routing_mix": True,
                }
                if batches[batch]:
                    request["local_evidence"] = list(batches[batch])
                try:
                    work = _post(client, project, f"{_BASE}/claim", request)
                except CollabError as error:
                    if error.code != "KNOWLEDGE_SCORING_ISOLATION":
                        raise
                    return {"status": "isolation_unavailable", "items": completed}
                status = str(work.get("status"))
                if status != "empty" or batch + 1 >= len(batches) or not enabled() or time.monotonic() >= stop:
                    break
                batch += 1
            if status != "open":
                break
            if work.get("attempt_id") != operation:
                raise CollabError("KNOWLEDGE_SCORE_ATTEMPT", "服务返回的评分领取标识无效。")
            outcome = _attempt_outcome(client, project, work, enabled, device, project_for)
            mixed = work.get("mixed_batch")
            routing_members = mixed is not None and any("routing" in row for row in items({"items": mixed}))
            if work.get("routing_batch") is not None or routing_members:
                endpoint = f"{_BASE}/routing"
            elif work.get("adoption") is not None:
                endpoint = "/adoption/attempts"
            elif work.get("adoption_batch") is not None:
                endpoint = "/adoption/batches"
            else:
                grouped = work.get("batch") is not None or work.get("mixed_batch") is not None
                endpoint = f"{_BASE}/batches" if grouped else f"{_BASE}/attempts"
            receipt = _post(client, project, f"{endpoint}/{operation}/settle", outcome)
            completed.append({"attempt_id": operation, **receipt})
            status = "processed"
    return {"status": status, "items": completed}
