"""只投影原生转录中可分享的文本，再改写本机路径和常见凭据。"""

import json
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import quote
from uuid import UUID

from weora_cli.collab_client import CollabError
from weora_cli.commands import JsonValue

_MAX_RAW = 50 * 1024 * 1024
_MAX_LINE = 2 * 1024 * 1024
_INJECTED = re.compile(r"(?is)<(?:system-reminder|task-notification)>.*?</(?:system-reminder|task-notification)>")
_CREDENTIAL = re.compile(
    r"(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"
    r"|\bBearer\s+[\w.+/=-]{8,}|\bBasic\s+[A-Za-z0-9+/=]{12,}"
    r"|\b(?:sk-|gh[pousr]_|github_pat_|glpat-|xox[baprs]-|xai-|gsk_|pplx-|dop_v1_|shpat_)[\w-]{12,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bAIza[A-Za-z0-9_-]{35}\b|\bya29\.[\w-]{20,}"
    r"|\b(?:npm_|hf_)[A-Za-z0-9]{16,}\b|\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
)
_CREDENTIAL_FIELD = re.compile(
    r"(?i)(\b\w*(?:token|password|passwd|(?:api|access|private)[_-]?key|secret|credential)\b[\"']?\s*[:=]\s*)"
    r"(?:\\?\"[^\"\r\n]+\"|\\?'[^'\r\n]+'|[A-Za-z0-9._+/%=-]{8,})",
)
_BASIC_URL = re.compile(r"(?i)(https?://)[^\s/@<>]+@")
_UNKNOWN_PATH = re.compile(
    r"""(?i)(?<![\w/])(?:[a-z]:[\\/]+|\\\\[^\\\s]+\\|/(?:[a-z]/)?"""
    r"""(?:Users|home|private/tmp|tmp|mnt|srv|workspace|workspaces|root)/)"""
    r"""[^\s<>\x00\"'`),;]+""",
)
_IMAGE = re.compile(r"(?i)data:image/[\w.+-]+;base64,[A-Za-z0-9+/=]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_SECRET_NAME = re.compile(r"(?:^|_)(?:TOKEN|PASSWORD|PASSWD|KEY|SECRET|CREDENTIAL|PAT)(?:$|_)", re.I)
_ROLES = {"user", "assistant"}
_TEXT_PARTS = {"text", "input_text", "output_text"}
_IMAGE_PARTS = {"image", "input_image", "image_url"}


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _native_rows(path: Path) -> Iterator[dict[str, JsonValue]]:
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_RAW:
            raise CollabError("KNOWLEDGE_TRANSCRIPT_LIMIT", "转录为空、过大或不是普通文件。")
        # 预览固定文件长度，正在追加的内容留给下一次预览。
        remaining = info.st_size
        while remaining and (line := stream.readline(min(remaining, _MAX_LINE + 1))):
            remaining -= len(line)
            if len(line) > _MAX_LINE:
                raise CollabError("KNOWLEDGE_TRANSCRIPT_LIMIT", "转录包含过长的单条记录。")
            try:
                yield _object(json.loads(line))
            except (ValueError, RecursionError):
                if remaining != 0:
                    raise CollabError("KNOWLEDGE_TRANSCRIPT_INVALID", "原生转录包含损坏记录。") from None


def _tool_text(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            block = _object(item)
            if isinstance(kind := block.get("type"), str):
                if kind in _TEXT_PARTS and isinstance(text := block.get("text"), str):
                    parts.append(text)
                elif kind in _IMAGE_PARTS:
                    parts.append("<image>")
        return "\n".join(parts)
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else str(value)


def _turns(message: dict[str, JsonValue]) -> Iterator[tuple[str, str]]:
    role, content = message.get("role"), message.get("content")
    if not isinstance(role, str) or role not in _ROLES:
        return
    if isinstance(content, str):
        yield str(role), content
    elif isinstance(content, list):
        text: list[str] = []
        for value in content:
            block = _object(value)
            kind = block.get("type")
            if isinstance(kind, str) and kind in _TEXT_PARTS and isinstance(segment := block.get("text"), str):
                text.append(segment)
            elif isinstance(kind, str) and kind in _IMAGE_PARTS:
                text.append("<image>")
            elif kind == "tool_use":
                if text:
                    yield str(role), "\n".join(text)
                    text.clear()
                yield "tool", f"{block.get('name', '工具')}\n{_tool_text(block.get('input'))}"
            elif kind == "tool_result":
                if text:
                    yield str(role), "\n".join(text)
                    text.clear()
                yield "tool", _tool_text(block.get("content"))
        if text:
            yield str(role), "\n".join(text)


def native_text(path: Path, runtime: str, session: UUID) -> tuple[str | None, list[tuple[str, str]]]:
    cwd = None
    owned = False
    turns: list[tuple[str, str]] = []
    try:
        for row in _native_rows(path):
            if runtime == "claude-code":
                if row.get("sessionId") != str(session) or row.get("isSidechain") is True:
                    continue
                owned = True
                if cwd is None and isinstance(folder := row.get("cwd"), str):
                    cwd = folder
                message = _object(row.get("message"))
                if row.get("type") in ("user", "assistant"):
                    turns.extend(_turns(message | {"role": row["type"]}))
            else:
                payload = _object(row.get("payload"))
                if row.get("type") == "session_meta":
                    if payload.get("id") != str(session) or isinstance(payload.get("source"), dict):
                        raise CollabError("KNOWLEDGE_TRANSCRIPT_SCOPE", "转录不属于所选根会话。")
                    owned = True
                    if isinstance(folder := payload.get("cwd"), str):
                        cwd = folder
                elif owned and row.get("type") == "response_item":
                    kind = payload.get("type")
                    if kind == "message":
                        turns.extend(_turns(payload))
                    elif kind in ("function_call", "custom_tool_call", "mcp_tool_call"):
                        arguments = _tool_text(payload.get("arguments", payload.get("input")))
                        turns.append(("tool", f"{payload.get('name', '工具')}\n{arguments}"))
                    elif kind in ("function_call_output", "custom_tool_call_output", "mcp_tool_call_output"):
                        turns.append(("tool", _tool_text(payload.get("output"))))
    except (OSError, RecursionError) as exc:
        raise CollabError("KNOWLEDGE_TRANSCRIPT_INVALID", "无法读取原生会话转录。") from exc
    if not owned or not turns:
        raise CollabError("KNOWLEDGE_TRANSCRIPT_SCOPE", "找不到可分享的根会话文本。")
    return cwd, turns


def _roots(cwd: str | None) -> list[tuple[str, str]]:
    roots = [(str(Path.home()), "~"), (gettempdir(), "<tmp>")]
    if cwd and len(cwd) <= 1000:
        path = Path(cwd)
        if path.is_absolute():
            repo = None if cwd.startswith(("//", "\\\\")) else next(
                (p for p in (path, *path.parents) if (p / ".git").exists()), None,
            )
            roots.append((str(repo or path), "<repo>" if repo else "<cwd>"))
    aliases: dict[str, str] = {}
    for root, marker in roots:
        root = root.rstrip("\\/")
        if len(root) < 3:
            continue
        slash = root.replace("\\", "/")
        for variant in (root, slash, root.replace("\\", "\\\\"), quote(slash, safe="/:")):
            aliases[variant] = marker
        if match := re.match(r"^([A-Za-z]):(/.*)", slash):
            aliases[f"/{match[1].lower()}{match[2]}"] = marker
    return sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)


def scrub_text(cwd: str | None, values: list[tuple[str, str]]) -> list[dict[str, JsonValue]]:
    aliases = _roots(cwd)
    environment = sorted(
        {value for key, value in os.environ.items() if len(value) >= 8 and _SECRET_NAME.search(key)},
        key=len, reverse=True,
    )
    result: list[dict[str, JsonValue]] = []
    for role, text in values:
        text = _CONTROL.sub("", _INJECTED.sub("", text))
        for secret in environment:
            text = text.replace(secret, "<redacted>")
        text = _CREDENTIAL_FIELD.sub(r"\1<redacted>", _CREDENTIAL.sub("<redacted>", text))
        text = _BASIC_URL.sub(r"\1", _IMAGE.sub("<image>", text))
        for root, replacement in aliases:
            flags = re.IGNORECASE if os.name == "nt" or re.match(r"^[A-Za-z]:", root) else 0
            text = re.sub(re.escape(root) + r"(?![A-Za-z0-9_.-])", replacement, text, flags=flags)
        text = _UNKNOWN_PATH.sub("<path>", text).strip()
        if text:
            result.append({"role": role, "text": text})
    return result
