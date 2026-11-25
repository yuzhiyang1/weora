"""会话经验提醒读取本机摩擦与召回计数，发布仍经确认。"""

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.collab_native import native_transcript, profile
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_friction import correction_prompt, correction_text, scan
from weora_cli.knowledge_recall_quality import recall_quality
from weora_cli.knowledge_settings import SessionSettings, session_state_root
from weora_cli.knowledge_workflow import SHARE_LEARNINGS

_IDLE = 60


def _read(path: Path) -> dict[str, JsonValue]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record(client: Client, runtime: str, session: UUID) -> Path:
    return session_state_root(client) / profile(runtime) / runtime / f"{session}.json"


def _corrections(state: dict[str, JsonValue]) -> int:
    value = state.get("corrections")
    return value if type(value) is int and value >= 0 else 0


def observe(settings: SessionSettings, runtime: str, event: Mapping[str, JsonValue]) -> str:
    """处理根会话结束与随后一分钟内的纠正，最多向宿主返回一次提醒。"""
    if runtime not in ("codex", "claude-code"):
        return ""
    if event.get("agent_id") or event.get("is_headless") is True or event.get("stop_hook_active") is True:
        return ""
    if runtime == "claude-code" and os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(("sdk", "headless")):
        return ""
    try:
        session = UUID(str(event.get("session_id")))
    except ValueError:
        return ""
    path = _record(settings.client, runtime, session)
    event_name = event.get("hook_event_name")
    now = int(time.time())
    if event_name == "UserPromptSubmit":
        prompt = event.get("prompt")
        if isinstance(prompt, str) and path.is_file() and settings.effective("share-reminder").get("enabled") is True:
            _note_correction(path, prompt, now)
        return ""
    transcript = event.get("transcript_path")
    if event_name != "Stop" or not isinstance(transcript, str):
        return ""
    state = _read(path)
    if state.get("hinted") or state.get("contributed"):
        return ""
    if settings.effective("share-reminder").get("enabled") is not True:
        return ""
    verified = native_transcript(runtime, session, transcript)
    if verified is None:
        return ""
    facts = scan(verified, "codex" if runtime == "codex" else "claude-code", str(session))
    if facts is None:
        return ""
    bonus, knowledge_gap = recall_quality(settings.client, str(session)).adjustment()
    with lock_file(path):
        state = _read(path)
        if state.get("hinted") or state.get("contributed"):
            return ""
        corrections = _corrections(state)
        state["stop_at"] = now
        if facts.eligible(corrections, bonus):
            state["hinted"] = True
        write_json(path, state)
    if state.get("hinted") is not True:
        return ""
    reasons = "、".join(filter(None, (facts.reason(corrections), knowledge_gap)))
    with suppress(CollabError):
        workflow = as_object(call(settings.client, None, f"/workflows/{SHARE_LEARNINGS}").get("definition"))
        invoke = workflow.get("invoke")
        if invoke in ("allow", "ask"):
            permission = "先询问本人是否启动，再" if invoke == "ask" else ""
            name = workflow.get("alias") or SHARE_LEARNINGS
            return (
                f"这次会话出现{reasons}。如果有值得复用的经验，{permission}"
                f"用 weora context workflow start {name} --runtime {runtime} --session {session}。"
                "发起时核对项目、会话归档和评分选择，并按返回的步骤继续。"
            )
    # 团队禁用 Agent 发起或无法读取策略时，只提示本人可以手工分享。
    return (
        f"这次会话出现{reasons}。如果有值得复用的经验，可以本人整理或核对 Markdown 草稿，"
        f"再用 weora context share-learnings --runtime {runtime} --session {session} --file <草稿路径> 预览，"
        "核对内容和项目范围后再确认发布。"
    )


def _note_correction(path: Path, prompt: str, now: int) -> None:
    prompt = correction_text(prompt)
    if not prompt:
        return
    with lock_file(path):
        state = _read(path)
        stop = state.pop("stop_at", None)
        if type(stop) is not int or state.get("hinted") or state.get("contributed"):
            return
        # 同一次 Stop 只检查随后收到的第一条真人提示词。
        if 0 <= now - stop <= _IDLE and correction_prompt(prompt):
            state["corrections"] = _corrections(state) + 1
        write_json(path, state)


def share_learnings(
    client: Client,
    *,
    runtime: str,
    session: str,
    project: str | None,
    files: list[Path],
    confirmation: str | None,
    read_markdown: Callable[[Path], tuple[str | None, list[str], str]],
) -> dict[str, JsonValue]:
    """审核实际发布内容，任一草稿变化后要求重新核对预览。"""
    if not files or len(files) > 25:
        raise CollabError("KNOWLEDGE_SHARE_LIMIT", "一次可预览或发布 1 至 25 份经验草稿。")
    source_ref = f"{runtime}:{session}"
    items: list[JsonValue] = []
    keys: set[str] = set()
    size = 0
    for file in files:
        title, tags, body = read_markdown(file)
        title = (title or file.stem).strip()
        tags = list(dict.fromkeys(tag.strip() for tag in tags))
        if (
            not 1 <= len(title) <= 160
            or not body.strip()
            or len(body) > 64_000
            or "\x00" in title
            or "\x00" in body
            or len(tags) > 24
            or any(not 1 <= len(tag.strip()) <= 60 or "\x00" in tag for tag in tags)
        ):
            raise CollabError("KNOWLEDGE_SHARE_INVALID", "请检查草稿的标题、正文和标签。")
        encoded = json.dumps([project, source_ref, title, tags, body], ensure_ascii=False, separators=(",", ":"))
        operation = str(uuid5(NAMESPACE_URL, f"weora:session-learning:{encoded}"))
        if operation in keys:
            raise CollabError("KNOWLEDGE_SHARE_DUPLICATE", "同一批次包含重复草稿。")
        keys.add(operation)
        size += len(body.encode("utf-8"))
        items.append(
            {
                "operation_id": operation,
                "kind": "learning",
                "title": title,
                "body": body,
                "tags": list(tags),
                "source": {"type": "session", "ref": source_ref},
            }
        )
    if size > 800_000:
        raise CollabError("KNOWLEDGE_SHARE_LIMIT", "草稿正文超过批次大小限制，请分批预览。")
    # 设备换绑时旧确认码失效，避免无项目的批次被发布到另一个工作空间。
    reviewed = json.dumps([session_state_root(client).name, project, items], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(reviewed.encode()).hexdigest()
    if confirmation is None:
        return {
            "preview": True,
            "workspace_id": client.workspace,
            "project": project,
            "confirm": digest,
            "items": items,
        }
    if confirmation != digest:
        raise CollabError("KNOWLEDGE_SHARE_CHANGED", "确认码与当前草稿或项目不符，请重新预览。")
    result = call(client, project, "/imports", method="POST", payload={"items": items})
    path = _record(client, runtime, UUID(session))
    with suppress(OSError), lock_file(path):
        write_json(path, {"contributed": True})
    return result
