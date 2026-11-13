"""设备凭据保存在操作系统钥匙串，HTTP 与 WSS 只连接已配对的 Weora。"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

import keyring

from weora_cli.commands import JsonValue

SERVICE = "Weora Agent Collaboration"


class CollabError(Exception):
    """可直接交给 Agent 或人类查看的业务错误。"""

    def __init__(self, code: str, message: str, data: JsonValue = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def config_path() -> Path:
    override = os.environ.get("WEORA_COLLAB_CONFIG")
    return Path(override) if override else Path.home() / ".weora" / "collaboration.json"


def write_json(path: Path, value: dict[str, JsonValue]) -> None:
    """先写临时文件，避免中断时留下半份用户配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        staged = Path(handle.name)
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


class _NoRedirect(HTTPRedirectHandler):
    # 设备 Bearer 只能发送给原先配对的服务，代理改地址时需重新配对。
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


def validate_server(base: str, *, allow_path: bool = False) -> str:
    base = base.strip().rstrip("/")
    try:
        url = urlsplit(base)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            url.hostname
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and (allow_path or not url.path)
            and (url.scheme == "https" or (url.scheme == "http" and local))
        ):
            _ = url.port
            return base
    except ValueError:
        pass
    raise CollabError("INVALID_SERVER", "请输入 HTTPS Weora 服务地址。本机开发可使用 HTTP 回环地址。")


def request_json(
    method: str,
    url: str,
    data: dict[str, JsonValue] | None = None,
    *,
    token: str = "",
    key: str | None = None,
    timeout: float = 12,
) -> dict[str, JsonValue]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    request = Request(
        url,
        data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return as_object(json.loads(response.read()) if response.status != 204 else {})
    except HTTPError as error:
        try:
            detail = as_object(as_object(json.loads(error.read())).get("detail"))
        except (ValueError, CollabError):
            detail = {}
        code = detail.get("code")
        message = detail.get("message")
        raise CollabError(
            str(code or f"HTTP_{error.code}"),
            str(message or f"Weora 请求失败（HTTP {error.code}）。"),
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise CollabError("WEORA_UNREACHABLE", "暂时无法连接 Weora。") from error


@dataclass(frozen=True)
class Client:
    base: str
    workspace: str
    token: str = ""
    timeout: float = 12

    def __post_init__(self) -> None:
        validate_server(self.base, allow_path=True)
        try:
            UUID(self.workspace)
        except ValueError as error:
            raise CollabError("INVALID_WORKSPACE", "工作空间 ID 格式无效。") from error

    @property
    def account(self) -> str:
        return f"{self.base}/{self.workspace}"

    @property
    def path(self) -> str:
        return f"{self.base.rstrip('/')}/api/v1/workspaces/{self.workspace}/agent-collaboration"

    @property
    def socket_url(self) -> str:
        url = urlsplit(self.path)
        return urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc, url.path + "/events", "", ""))

    def request(
        self,
        method: str,
        path: str,
        data: dict[str, JsonValue] | None = None,
        *,
        key: str | None = None,
    ) -> dict[str, JsonValue]:
        return request_json(method, self.path + path, data, token=self.token, key=key, timeout=self.timeout)


def as_object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise CollabError("INVALID_RESPONSE", "Weora 返回的数据格式无效。")
    return value


def items(value: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    rows = value.get("items")
    if not isinstance(rows, list):
        raise CollabError("INVALID_RESPONSE", "Weora 返回的列表格式无效。")
    return [as_object(row) for row in rows]


def read_config() -> dict[str, JsonValue]:
    try:
        return as_object(json.loads(config_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def paired_client(*, timeout: float = 12) -> Client:
    raw = read_config()
    base = os.environ.get("WEORA_COLLAB_URL", str(raw.get("base") or "")).rstrip("/")
    workspace = os.environ.get("WEORA_COLLAB_WORKSPACE", str(raw.get("workspace") or ""))
    device_id = str(raw.get("device_id") or "")
    if not base or not workspace:
        raise CollabError("DEVICE_NOT_PAIRED", "请先运行 weora login --url <Weora 服务地址>。")
    # 环境变量只用于调用方已管理的密钥注入，例如隔离的集成测试。
    account = f"{base}/{workspace}"
    if device_id:
        account = f"{account}/{device_id}"
    token = os.environ.get("WEORA_COLLAB_TOKEN") or keyring.get_password(SERVICE, account)
    if not token:
        raise CollabError("DEVICE_NOT_PAIRED", "系统钥匙串中没有此设备凭据，请重新配对。")
    return Client(base, workspace, token, timeout)


def pair(base: str, workspace: str, code: str) -> dict[str, JsonValue]:
    client = Client(base.rstrip("/"), workspace)
    result = client.request("POST", "/devices/pair", {"code": code})
    secret = result.pop("token", None)
    device_id = result.get("id")
    if not isinstance(secret, str) or not isinstance(device_id, str):
        raise CollabError("INVALID_RESPONSE", "Weora 没有返回设备凭据。")
    save_device(client, device_id, secret)
    return result


def save_device(client: Client, device_id: str, secret: str) -> None:
    # 同一个系统账户可能并行连接同一工作空间的多个测试设备。
    keyring.set_password(SERVICE, f"{client.account}/{device_id}", secret)
    write_json(config_path(), {"base": client.base, "workspace": client.workspace, "device_id": device_id})
