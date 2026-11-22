"""从客户端实际可见的安装代次提取会话评分所需的描述。"""

import hashlib
import json
from pathlib import Path
from sqlite3 import Connection
from typing import TypedDict
from uuid import UUID

from ruamel.yaml.error import YAMLError

from weora_cli.collab_client import CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_markdown import parse_metadata
from weora_cli.knowledge_transcript_text import scrub_text
from weora_cli.skill_adapters import skill_roots, tool_home
from weora_cli.skill_local import current_bindings, digest, unsafe, usage_session_key


class CapturedDeliveries(TypedDict):
    items: list[dict[str, JsonValue]]
    scopes: list[str | None]
    usage: dict[str, JsonValue] | None


def _captured_description(target: Path, expected: object, cwd: Path) -> tuple[bool, str | None]:
    marker = target / "SKILL.md"
    if unsafe(target) or unsafe(marker) or not marker.is_file() or not isinstance(expected, str):
        return False, None
    with marker.open("rb") as stream:
        # 只保留头部解析描述，完整文件以流式摘要核对安装回执。
        body = stream.read(128 * 1024)
        content_hash = hashlib.sha256(body)
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            content_hash.update(chunk)
    if content_hash.hexdigest() != expected:
        return False, None
    try:
        lines = body.decode("utf-8-sig").splitlines()
        end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
        metadata = parse_metadata("\n".join(lines[1:end]), rule=False) if end and lines[0].strip() == "---" else None
    except (ValueError, YAMLError, RecursionError):
        return True, None
    raw = metadata.get("description") if isinstance(metadata, dict) else None
    text = raw.strip()[:1024] if isinstance(raw, str) else ""
    clean = scrub_text(str(cwd), [("user", text)]) if text else []
    return True, str(clean[0]["text"]) if clean else None


def delivered_at(db: Connection, runtime: str, session: str, cwd: Path, observed: str) -> CapturedDeliveries | None:
    """只记录已验证安装目录从真实 Skill 文件可见的交付元数据。"""
    bindings = current_bindings(db, runtime, cwd)
    if not bindings:
        return None
    checkout = next((level for level in (cwd, *cwd.parents) if (level / ".git").exists()), None)
    selected: dict[str, dict[str, JsonValue]] = {}
    confirmed: dict[str, set[Path]] = {}
    visible: dict[str, set[Path]] = {}
    versions: dict[str, set[str]] = {}
    for binding in bindings:
        root = Path(binding["root"]) if binding["root"] else checkout
        for row in db.execute("SELECT * FROM installation WHERE binding=? ORDER BY slug", (binding["id"],)):
            name = str(row["slug"])
            target = Path(row["target"])
            marker = (target / "SKILL.md").resolve()
            candidates = {item / name / "SKILL.md" for item in skill_roots(runtime, cwd, root)}
            visible.setdefault(name, set()).update(path.resolve() for path in candidates if path.is_file())
            if marker not in visible[name]:
                continue
            try:
                files = as_object(json.loads(row["files"]))
                verified, description = _captured_description(target, files.get("SKILL.md"), cwd)
                generation = str(UUID(row["generation"]))
            except (ValueError, OSError, CollabError):
                continue
            if not verified:
                continue
            confirmed.setdefault(name, set()).add(marker)
            versions.setdefault(name, set()).add(str(row["version_id"]))
            selected.setdefault(name, {
                "generation": generation, "description": description, "observed_at": observed,
                "project": binding["project_id"],
            })
    usage: dict[str, JsonValue] | None = None
    if any(binding["usage"] for binding in bindings):
        profile = digest(str(tool_home(runtime)).encode())
        secret = bytes.fromhex(db.execute("SELECT value FROM secret WHERE id=1").fetchone()[0])
        usage = {"usage_profile": profile, "usage_session": usage_session_key(secret, runtime, profile, session)}
    return {
        # 未受管理的同名副本或不同命名空间的同名包会造成路由歧义。
        "items": [selected[name] for name in sorted(selected) if confirmed[name] == visible[name]
                  and len(versions[name]) == 1],
        "scopes": [binding["project_id"] for binding in bindings],
        "usage": usage,
    }
