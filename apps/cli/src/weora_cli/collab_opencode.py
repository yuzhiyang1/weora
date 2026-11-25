"""OpenCode 1.x 本机 Server 接入，适用于已经运行且没有加载插件的服务。"""

import base64
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from weora_cli.collab_client import Client, CollabError, _NoRedirect, as_object
from weora_cli.collab_native import WakeState
from weora_cli.collaboration import open_local_session


def run_opencode(client: Client, url: str, native: str, name: str) -> None:
    target = urlsplit(url)
    if (
        target.scheme not in {"http", "https"}
        or target.hostname not in {"localhost", "127.0.0.1", "::1"}
        or target.username is not None
        or target.password is not None
        or target.path not in {"", "/"}
        or target.query
        or target.fragment
        or not re.fullmatch(r"ses_[A-Za-z0-9]+", native)
    ):
        raise CollabError("OPENCODE_TARGET_INVALID", "请输入本机 OpenCode 服务地址及准确的 ses_ 会话 ID。")
    headers = {"Content-Type": "application/json"}
    if password := os.environ.get("OPENCODE_SERVER_PASSWORD"):
        username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {credentials}"
    base = url.rstrip("/") + f"/session/{native}"
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(Request(base, headers=headers), timeout=8) as response:
            session = as_object(json.load(response))
            if session.get("id") != native or session.get("parentID"):
                raise CollabError("OPENCODE_TARGET_INVALID", "只能连接已存在的 OpenCode 主会话。")
    except (OSError, ValueError) as error:
        raise CollabError("OPENCODE_SESSION_UNAVAILABLE", "未能连接指定的 OpenCode 会话。") from error

    def deliver(hint: str) -> WakeState:
        body = json.dumps({"parts": [{"type": "text", "text": hint}]}).encode()
        try:
            with opener.open(Request(base + "/prompt_async", data=body, headers=headers), timeout=8) as response:
                return "accepted" if response.status == 204 else "unknown"
        except HTTPError as error:
            return "failed" if 400 <= error.code < 500 else "unknown"
        except OSError:
            return "unknown"

    open_local_session(client, "opencode", native, name, deliver)
