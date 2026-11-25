"""按环境、目录成员偏好和工作空间策略解析本次会话的行为。"""

import hashlib
import json
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

from weora_cli.collab_client import Client, CollabError, config_path, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call

SessionPolicy = Literal["recall", "share-reminder"]
_API = "/settings/session-context"
_ENV = {"recall": "WEORA_RECALL_DISABLED", "share-reminder": "WEORA_SHARE_REMINDER_DISABLED"}


def session_state_root(client: Client) -> Path:
    fingerprint = hashlib.sha256(f"{client.account}\0{client.token}".encode()).hexdigest()
    return config_path().parent / "knowledge-session" / fingerprint


def _preferences(path: Path) -> dict[str, JsonValue]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise CollabError("KNOWLEDGE_SETTINGS_INVALID", "设备上的会话策略格式无效。")
    return value


@dataclass(frozen=True)
class SessionSettings:
    client: Client
    cwd: Path

    def _path(self, kind: SessionPolicy) -> Path:
        root = session_state_root(self.client)
        # 延用已经发布的设备目录配置，原覆盖值无需重新设定。
        return root.parent.parent / "recall" / f"{root.name}.json" if kind == "recall" else root / "settings.json"

    def _local(self, kind: SessionPolicy) -> dict[str, JsonValue]:
        path = self.cwd.resolve()
        entries = _preferences(self._path(kind))
        local: dict[str, JsonValue] = {}
        for directory in (*reversed(path.parents), path):
            value = entries.get(str(directory))
            if kind == "share-reminder" and isinstance(value, bool):
                local["enabled"] = value
            elif kind == "recall" and isinstance(value, dict):
                local.update(value)
        return local

    @cached_property
    def _workspace(self) -> dict[str, JsonValue] | CollabError:
        # 同一个 Hook 的召回和分享门槛共用这次响应。
        try:
            return call(self.client, None, _API)
        except CollabError as error:
            return error

    def _team(self, kind: SessionPolicy, *, strict: bool) -> dict[str, JsonValue]:
        workspace = self._workspace
        if isinstance(workspace, CollabError):
            if strict:
                raise workspace
            # 取不到管理员策略时停止继承的自动行为，手工检索可以照常调用。
            return {"enabled": False, "source": "unavailable"}
        value = workspace.get(kind)
        if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
            return value
        if strict:
            raise CollabError("KNOWLEDGE_SETTINGS_INVALID", "服务端返回的会话策略格式无效。")
        return {"enabled": kind == "share-reminder", "source": "default"}

    def effective(self, kind: SessionPolicy) -> dict[str, JsonValue]:
        if os.environ.get(_ENV[kind]) == "1":
            return {"enabled": False, "source": "environment"}
        if kind == "share-reminder" and self.effective("recall").get("enabled") is not True:
            return {"enabled": False, "source": "recall"}
        local = self._local(kind)
        if isinstance(local.get("enabled"), bool):
            return local | {"source": "local"}
        team = self._team(kind, strict=False)
        return team | ({"project": local["project"]} if "project" in local else {})

    def recall_project(self) -> str | None:
        """手工召回也沿用当前目录的项目关联，独立于自动召回开关。"""
        project = self._local("recall").get("project")
        return project if isinstance(project, str) else None

    def configure(
        self,
        kind: SessionPolicy,
        mode: str,
        *,
        workspace: bool = False,
        project: str | None = None,
    ) -> dict[str, JsonValue]:
        if workspace:
            if project is not None or mode == "inherit":
                raise CollabError("KNOWLEDGE_SETTINGS_SCOPE", "工作空间默认值支持 enable、disable 和 status。")
            if mode == "status":
                return self._team(kind, strict=True)
            return call(self.client, None, f"{_API}/{kind}", method="PUT", payload={"enabled": mode == "enable"})
        if mode != "status":
            if kind == "recall" and project is not None:
                # 保存目录关联前验证同一账号当前可读取的项目。
                call(self.client, project, query="", limit=1)
            path = self._path(kind)
            key = str(self.cwd.resolve())
            with lock_file(path):
                entries = _preferences(path)
                if mode == "inherit":
                    if kind == "recall" and project is not None:
                        entries[key] = {"project": project}
                    else:
                        entries.pop(key, None)
                elif kind == "recall":
                    entries[key] = {"enabled": mode == "enable", "project": project}
                else:
                    entries[key] = mode == "enable"
                write_json(path, entries)
        return self.effective(kind)
