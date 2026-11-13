"""在浏览器确认工作空间，再通过系统钥匙串持久化设备身份。"""

import argparse
import json
import os
import socket
import sys
import time
import webbrowser
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

import keyring
from keyring.errors import PasswordDeleteError

from weora_cli.collab_client import (
    SERVICE,
    Client,
    CollabError,
    as_object,
    config_path,
    paired_client,
    read_config,
    request_json,
    save_device,
    validate_server,
    write_json,
)
from weora_cli.commands import JsonValue


def arguments(parser: argparse.ArgumentParser, operation: str) -> None:
    if operation == "auth":
        actions = parser.add_subparsers(dest="auth_action", required=True)
        for action in ("login", "status", "logout"):
            arguments(actions.add_parser(action), action)
    elif operation == "login":
        parser.add_argument("--url", help="Weora 服务地址")
        parser.add_argument("--name", help="浏览器里显示的设备名称")
        parser.add_argument("--no-browser", action="store_true", help="仅显示链接，不自动打开浏览器")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--no-wait", action="store_true", help="返回授权链接和请求 ID，稍后继续")
        mode.add_argument("--resume", type=UUID, help="继续本机已发起的登录请求")


def _pending_key(login_id: UUID) -> str:
    return f"pending-cli-login:{login_id}"


def _forget(account: str) -> None:
    try:
        keyring.delete_password(SERVICE, account)
    except PasswordDeleteError:
        # 另一个终端可能已经处理同一请求。
        if keyring.get_password(SERVICE, account) is not None:
            raise


def _login_path(base: str) -> str:
    return f"{base}/api/v1/cli-auth/requests"


def _start(base: str, name: str) -> tuple[UUID, dict[str, JsonValue]]:
    verifier = token_urlsafe(32)
    begun = request_json(
        "POST",
        _login_path(base),
        {"name": name, "verifier_hash": sha256(verifier.encode()).hexdigest()},
    )
    login_id = UUID(str(begun["id"]))
    lifetime = begun["expires_in"]
    interval = begun["interval"]
    code = begun["code"]
    if (
        not isinstance(lifetime, int)
        or not 0 < lifetime <= 600
        or not isinstance(interval, int)
        or not isinstance(code, str)
    ):
        raise CollabError("INVALID_RESPONSE", "Weora 返回了无效的登录请求。")
    pending: dict[str, JsonValue] = {
        "base": base,
        "verifier": verifier,
        "deadline": time.time() + lifetime,
        "interval": interval,
        "code": code,
        "expires_in": lifetime,
    }
    # 恢复命令使用请求 ID，兑换验证串只保存在系统钥匙串。
    keyring.set_password(SERVICE, _pending_key(login_id), json.dumps(pending))
    return login_id, pending


def _finish(login_id: UUID, pending: dict[str, JsonValue]) -> dict[str, JsonValue]:
    base = validate_server(str(pending["base"]))
    deadline = pending["deadline"]
    interval = pending["interval"]
    if not isinstance(deadline, int | float) or not isinstance(interval, int):
        raise CollabError("CLI_LOGIN_UNAVAILABLE", "本机登录请求无效，请重新发起。")
    interval = max(1, min(interval, 30))
    while time.time() < deadline:
        try:
            result = request_json(
                "POST",
                f"{_login_path(base)}/{login_id}/poll",
                {"verifier": str(pending["verifier"])},
                timeout=min(12, max(1, deadline - time.time())),
            )
        except CollabError as exc:
            if exc.code == "CLI_LOGIN_UNAVAILABLE":
                break
            if exc.code != "WEORA_UNREACHABLE":
                raise
        else:
            status = result.get("status")
            if status == "authorized":
                token = result.pop("token", None)
                if not isinstance(token, str) or not token:
                    raise CollabError("INVALID_RESPONSE", "Weora 未返回设备凭据。")
                client = Client(base, str(result["workspace_id"]))
                device_id = str(UUID(str(result["id"])))
                previous = read_config()
                if previous.get("device_id") != device_id:
                    # 撤销当前配置的旧设备；同一请求重复兑换时保留该设备。
                    _clear_saved_device(previous, replacement_base=base)
                save_device(client, device_id, token)
                _forget(_pending_key(login_id))
                return result | {"server": base}
            if status == "denied":
                _forget(_pending_key(login_id))
                raise CollabError("CLI_LOGIN_DENIED", "浏览器拒绝了登录请求。")
            if status == "expired":
                break
            if status == "slow_down":
                # 与设备授权的轮询约定一致，服务端要求降速时延长后续间隔。
                suggested = result.get("interval")
                interval = min(30, max(interval + 5, suggested if isinstance(suggested, int) else 0))
            if status not in {"pending", "slow_down"}:
                raise CollabError("INVALID_RESPONSE", "Weora 返回了未知的登录状态。")
        time.sleep(min(interval, max(0, deadline - time.time())))
    _forget(_pending_key(login_id))
    raise CollabError("CLI_LOGIN_EXPIRED", "登录请求已过期，请重新发起。")


def _login(args: argparse.Namespace) -> dict[str, JsonValue]:
    if args.resume:
        stored = keyring.get_password(SERVICE, _pending_key(args.resume))
        if not stored:
            raise CollabError("CLI_LOGIN_UNAVAILABLE", "系统钥匙串中找不到该登录请求，请重新发起。")
        return _finish(args.resume, as_object(json.loads(stored)))
    base = args.url or os.environ.get("WEORA_URL") or read_config().get("base")
    if not isinstance(base, str) or not base:
        raise CollabError("INVALID_SERVER", "请提供 --url <Weora 服务地址>。")
    base = validate_server(base)
    login_id, pending = _start(base, args.name or socket.gethostname()[:80] or "Weora CLI")
    browser_url = f"{base}/cli/authorize/{login_id}"
    if args.no_wait:
        return {
            "request_id": str(login_id),
            "verification_url": browser_url,
            "code": pending["code"],
            "status": "pending",
            "expires_in": pending["expires_in"],
        }
    print(f"请打开 {browser_url}\n核对验证码 {pending['code']} 后确认登录。", file=sys.stderr, flush=True)
    if not args.no_browser:
        webbrowser.open(browser_url)
    return _finish(login_id, pending)


def _clear_saved_device(config: dict[str, JsonValue], *, replacement_base: str | None = None) -> None:
    base, workspace, device_id = (config.get(key) for key in ("base", "workspace", "device_id"))
    if not isinstance(base, str) or not isinstance(workspace, str):
        return
    account = f"{base}/{workspace}" + (f"/{device_id}" if isinstance(device_id, str) else "")
    token = keyring.get_password(SERVICE, account)
    if not token:
        return

    # 退出和同站点换号会撤销远端设备。换站点只清除本机旧凭据，避免依赖旧站点在线。
    if replacement_base is None or replacement_base == base:
        try:
            Client(base, workspace, token).request("DELETE", "/device-self")
        except CollabError as exc:
            if exc.code != "AGENT_DEVICE_UNAUTHORIZED":
                raise
    _forget(account)


def _logout() -> dict[str, JsonValue]:
    config = read_config()
    _clear_saved_device(config)
    if isinstance(base := config.get("base"), str):
        # 保留服务地址，方便用户在当前配置重新登录。
        write_json(config_path(), {"base": base})
    return {"logged_out": True}


def execute(args: argparse.Namespace) -> dict[str, JsonValue]:
    action = args.auth_action if args.operation == "auth" else args.operation
    if action == "login":
        return _login(args)
    if action == "logout":
        return _logout()
    try:
        client = paired_client()
        return client.request("GET", "/device-self") | {"authenticated": True, "server": client.base}
    except CollabError as exc:
        if exc.code in {"DEVICE_NOT_PAIRED", "AGENT_DEVICE_UNAUTHORIZED"}:
            return {"authenticated": False}
        raise
