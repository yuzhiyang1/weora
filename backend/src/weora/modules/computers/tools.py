"""模型可在首次打开测试站点时准备电脑。人工接管与停止由控制台处理。"""

import asyncio
import base64
import binascii
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from weora.modules.computers.credentials import ComputerCredentials
from weora.modules.computers.openbot import ComputerError, OpenBot
from weora.modules.computers.policy import ComputerMode, computer_mode
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunError
from weora.modules.tools.application.gateway import ToolAccess, ToolGateway, ToolScope, ToolSpec, failure


def register_computer_tools(
    gateway: ToolGateway,
    access: ToolAccess,
    runs: RunService,
    bot: OpenBot,
    origins: tuple[str, ...],
    feishu_scopes: dict[str, ComputerMode],
    delivery: DeliveryService,
    credentials: ComputerCredentials | None = None,
) -> None:
    text = {"type": "string", "minLength": 1, "maxLength": 2048}
    reference = {"type": "string", "minLength": 1, "maxLength": 100}
    snapshot = {"type": "integer", "minimum": 1}
    actions: tuple[tuple[str, str, dict[str, object]], ...] = (
        ("navigate", "打开管理员登记的测试站点，首次按需启动电脑。人工停止后由人类重新启动。", {"url": text}),
        ("snapshot", "读取当前浏览器页面结构和快照号。人工接管时等待交还，交还后先用此工具刷新页面。", {}),
        ("read", "读取当前页面可见正文，包括静态标题。不能把快照未列出的文字判断为网站未渲染。", {}),
        ("credentials", "列出当前用户为本话题登记的测试凭据引用，不返回账号密码。", {}),
        ("validate_evidence", "校验截图归属、哈希和保留期；仅验证观察产物，不替代代码交付测试或审批。", {
            "artifact_ids": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
                             "items": {"type": "string", "format": "uuid"}},
        }),
        ("fill_credential", "用已登记测试凭据填充登录字段，模型不接触明文；提交登录须另行点击。", {
            "credential_id": {"type": "string", "format": "uuid"}, "ref": reference, "snapshotId": snapshot,
            "field": {"enum": ["username", "password"]},
        }),
        ("request_human", "请求用户接管登录或验证码；随后报告 blocked 和协助步骤，等待用户交还后续接。", {
            "reason": {"type": "string", "minLength": 1, "maxLength": 200},
        }),
        ("evidence", "采集网页截图与最近请求的路径、状态和失败事实。用于验收，结果会返回截图证据编号。", {}),
        ("click", "点击最新页面快照中的元素。", {"ref": reference, "snapshotId": snapshot}),
        ("type", "在最新页面快照的元素中输入文字。", {"ref": reference, "snapshotId": snapshot, "text": text}),
    )
    for action, description, properties in actions:
        effect = "read" if action in {"snapshot", "read", "evidence", "credentials", "validate_evidence"} else "write"

        async def handle(
            scope: ToolScope, args: dict[str, object], *, action: str = action, effect: str = effect
        ) -> dict[str, object]:
            dispatched = False
            try:
                detail = await runs.get(actor=scope.actor, workspace_id=scope.actor.workspace_id, run_id=scope.run_id)
                mode = computer_mode(detail.task, feishu_scopes)
                if mode is None:
                    return failure("COMPUTER_NOT_ALLOWED")
                if mode == "observe" and action in {"click", "type", "credentials", "fill_credential", "request_human"}:
                    return failure("COMPUTER_OBSERVE_ONLY")
                origin_metadata = detail.task.origin_metadata or {}
                credential_scope = str(origin_metadata.get("coordination_scope_id", ""))
                if action == "credentials":
                    items = await credentials.available(scope.actor, credential_scope) if credentials else []
                    return {"ok": True, "data": {"credentials": items}}
                if action == "validate_evidence":
                    images = await delivery.reply_screenshots(
                        scope.actor, scope.run_id, artifact_ids=tuple(cast(list[str], args["artifact_ids"]))
                    )
                    return {"ok": True, "data": {"validated": True, "scope": "screenshot_provenance",
                        "artifacts": [{"id": str(item.id), "sha256": item.sha256} for item in images]}}
                if action == "navigate":
                    url = urlsplit(str(args["url"]))
                    if url.username or url.password or f"{url.scheme}://{url.netloc}" not in origins:
                        return failure("COMPUTER_ORIGIN_DENIED")
                identity = bot.computer_id(
                    scope.actor.workspace_id, scope.actor.user_id, detail.task.id, observe=mode == "observe"
                )
                state = await bot.state(identity)
                if state is None and action == "navigate":
                    # 只有白名单内的首次导航可按需启动，不能重新开启被用户停止的电脑。
                    dispatched = True
                    await bot.supervisor(f"/computers/{identity}/ensure", {})
                elif state != "running":
                    return failure("COMPUTER_STOPPED")
                # 人工接管时也不读取网页正文，用户可能正在输入。等待期间复验 Worker 租约。
                try:
                    async with asyncio.timeout(45):
                        while True:
                            if not await access.authorize(scope, effect=effect):
                                return failure("ACCESS_REVOKED")
                            control = await bot.call(identity, "/control")
                            request = control.get("request")
                            pending = isinstance(request, dict) and request.get("status") == "waiting"
                            if (control.get("holder") == "bot" and not control.get("transitioning")
                                and (not pending or action == "request_human")):
                                break
                            await asyncio.sleep(1)
                except TimeoutError:
                    return failure("COMPUTER_HUMAN_CONTROL")

                if not await access.authorize(scope, effect=effect):
                    return failure("ACCESS_REVOKED")
                payload = (
                    {**args, "observeOrigins": list(origins)} if action == "navigate" and mode == "observe" else args
                )
                endpoint = f"/{action}"
                if action == "fill_credential":
                    if credentials is None:
                        return failure("COMPUTER_CREDENTIAL_DENIED")
                    current = await bot.call(identity, "/read")
                    location = urlsplit(str(current.get("url", "")))
                    origin = f"{location.scheme}://{location.netloc}"
                    if origin not in origins:
                        return failure("COMPUTER_ORIGIN_DENIED")
                    value = await credentials.value(
                        scope.actor, credential_scope, UUID(str(args["credential_id"])), origin, str(args["field"])
                    )
                    if not await access.authorize(scope, effect=effect):
                        return failure("ACCESS_REVOKED")
                    # 只把引用写入工具日志，明文仅在本地应用与电脑服务的私有请求中出现。
                    payload = {"ref": args["ref"], "snapshotId": args["snapshotId"],
                               "field": args["field"], "origin": origin, "text": value}
                    endpoint = "/credential"
                elif action == "request_human":
                    endpoint = "/control/request"
                dispatched = True
                result = await bot.call(
                    identity,
                    endpoint,
                    None if action in {"evidence", "read"} else payload,
                    deadline_seconds=40 if action == "navigate" else 12,
                )
                if action == "request_human":
                    return {"ok": True, "data": {
                        "requested": True, "run_id": str(scope.run_id),
                        "console_path": f"/runs/{scope.run_id}",
                        "workspace_id": str(scope.actor.workspace_id),
                        "next": "报告 blocked，请用户打开运行的虚拟电脑页签接管；交还后在原飞书话题回复继续。",
                    }}
                if action == "navigate" and result.get("navigation_failed") is True:
                    return failure("BROWSER_NAVIGATION_FAILED")
                if action == "evidence":
                    encoded = result.pop("screenshot", None)
                    result["screenshot_available"] = False
                    result["url_query_omitted"] = True
                    if isinstance(encoded, str) and len(encoded) <= 254_000:
                        try:
                            data = base64.b64decode(encoded, validate=True)
                        except binascii.Error:
                            return failure("COMPUTER_PROTOCOL")
                        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                            return failure("COMPUTER_PROTOCOL")
                        if not await access.authorize(scope, effect=effect):
                            return failure("ACCESS_REVOKED")
                        artifact = await delivery.record_computer_screenshot(scope.actor, scope.run_id, encoded)
                        result["screenshot_available"] = True
                        result["screenshot_artifact_id"] = str(artifact.id)
                        result["screenshot_sha256"] = artifact.sha256
                return {"ok": True, "data": result}
            except ComputerError as error:
                if action == "navigate" and error.code == "COMPUTER_UPSTREAM_FAILED":
                    # OpenBot 已明确结束导航并返回 502，浏览器仍可取快照核对当前页。
                    # 请求本身超时或断连仍按下方 UNKNOWN 处理，避免盲目重放。
                    return failure("BROWSER_NAVIGATION_FAILED")
                if dispatched and effect != "read" and error.status >= 500:
                    # 网关把结果不明的写操作记为 UNKNOWN，Worker 不会自动重放。
                    raise RuntimeError("电脑操作结果需要核对") from error
                return failure(error.code)
            except RunError:
                return failure("COMPUTER_NOT_ALLOWED")
            except DeliveryError as error:
                return failure(error.code)
            except ValueError:
                return failure("TOOL_ARGUMENTS_INVALID")

        gateway.register(
            ToolSpec(
                "browser." + action,
                description,
                {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
                effect=effect,
                timeout_seconds=175 if action == "navigate" else 64,
            ),
            handle,
        )
