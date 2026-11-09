"""显式注册允许的交付 Tool；审批动作故意不在该表中。"""

from typing import cast

from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import PLAN_ACTIONS, ArtifactRecord, DeliveryError, PlanScope
from weora.modules.tools.application.gateway import ToolGateway, ToolScope, ToolSpec


def _object(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _artifact_result(artifact: ArtifactRecord, screenshot: ArtifactRecord | None = None) -> dict[str, object]:
    """文本Runtime只得到真实截图的显式引用，永远不经工具结果接收图像编码。"""
    result: dict[str, object] = {
        "ok": True,
        "artifact_id": str(artifact.id),
        "kind": artifact.kind,
        "status": artifact.status,
        "body": artifact.body,
        "exit_code": artifact.exit_code,
        "repository_sha256": artifact.repository_sha256,
    }
    if artifact.kind == "browser":
        result["screenshot_artifact_id"] = str(screenshot.id) if screenshot else None
        result["screenshot_status"] = screenshot.status if screenshot else None
    return result


def register_delivery_tools(gateway: ToolGateway, service: DeliveryService) -> None:
    """角色与当前审批都由可信 Worker 决定，模型参数不能切换角色。"""
    text: dict[str, object] = {"type": "string", "minLength": 1, "maxLength": 12000}
    scope_schema = _object(
        {
            "repository_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "base_revision": {"type": "string", "pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$"},
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
            "commands": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "maxItems": 30},
            "actions": {
                "type": "array",
                "items": {"type": "string", "enum": list(PLAN_ACTIONS)},
                "minItems": 1,
                "maxItems": len(PLAN_ACTIONS),
                "description": (
                    "填写审批权限名，不是工具名。sandbox.file.write 需要 file.write；"
                    "sandbox.command 需要 command.run；sandbox.diff 需要 git.diff；"
                    "browser.verify 需要同名权限。交付分支需要 git.commit、git.push 分别批准；"
                    "草稿 PR 另需 pr.create；普通 MR 使用独立 review.create 计划。"
                    "只请求用户已授权且本次确实需要的动作，不能自行增加推送或 MR。"
                ),
            },
            "remote_id": {"type": ["string", "null"], "maxLength": 100},
            "target_branch": {"type": ["string", "null"], "maxLength": 120},
            "source_delivery_id": {
                "type": ["string", "null"],
                "maxLength": 36,
                "description": (
                    "普通 MR 引用成功分支；续写同一 Task 的原 PR 时引用最近成功分支交付 ID，"
                    "重新审批 git.commit/git.push，不重复申请 pr.create。"
                ),
            },
        },
        ["repository_id", "base_revision", "paths", "commands", "actions"],
    )
    definitions = [
        ToolSpec(
            "plan.propose",
            "提出研发计划等待人工审批；不能自行批准或扩大执行范围。",
            _object({"scope": scope_schema, "summary": text, "risk": text}, ["scope", "summary", "risk"]),
            effect="control",
        ),
        ToolSpec(
            "sandbox.command",
            "需要 command.run 审批权限，在已批准的隔离环境执行精确匹配的命令 argv。",
            _object({"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 64}}, ["argv"]),
            effect="write",
            timeout_seconds=610,
        ),
        ToolSpec(
            "sandbox.file.write",
            "需要 file.write 审批权限，只向批准路径写入文件；不能写宿主机或 Git 元数据。",
            _object({"path": text, "content": {"type": "string", "maxLength": 60000}}, ["path", "content"]),
            effect="write",
            timeout_seconds=610,
        ),
        ToolSpec(
            "sandbox.diff",
            "需要 git.diff 审批权限，验证改动没有越过批准范围并保存真实 Diff。",
            _object({}, []),
            effect="write",
            timeout_seconds=610,
        ),
        ToolSpec(
            "browser.verify",
            "验证本Run服务；artifact_id是验证记录，只有screenshot_artifact_id才是截图引用；不接受任意URL。",
            _object({"path": text, "expected_text": text}, ["path", "expected_text"]),
            effect="write",
            timeout_seconds=610,
        ),
        ToolSpec(
            "delivery.branch.push",
            "提交和推送分别获批且测试/Diff通过后发布专用分支；"
            "指定来源交付时按冻结 SHA 续写原 PR 分支，不创建 MR、不合并。",
            _object({"title": {"type": "string", "minLength": 1, "maxLength": 200}}, ["title"]),
            effect="write",
            timeout_seconds=610,
        ),
        ToolSpec(
            "delivery.review.create",
            "仅在 review.create 单独获批后，为已交付分支创建普通 MR（非草稿）；不重新提交、不推送、不合并。",
            _object({"title": {"type": "string", "minLength": 1, "maxLength": 200}, "body": text}, ["title", "body"]),
            effect="write",
            timeout_seconds=120,
        ),
        ToolSpec(
            "delivery.pr.create",
            "仅在提交、推送、远端和目标分支分别获批后创建草稿 PR；未知结果先对账。",
            _object({"title": {"type": "string", "minLength": 1, "maxLength": 200}, "body": text}, ["title", "body"]),
            effect="write",
            timeout_seconds=610,
        ),
        ToolSpec(
            "delivery.pr.reconcile",
            "只读核对本 Run 已请求的 MR/PR（普通或草稿）；查不到保持待恢复，绝不提交、推送或新建。",
            _object({}, []),
            effect="read",
            timeout_seconds=60,
        ),
    ]

    async def execute(scope: ToolScope, name: str, arguments: dict[str, object]) -> dict[str, object]:
        try:
            if name == "delivery.pr.reconcile":
                result = await service.reconcile_pr(scope.actor, scope.run_id)
                return {
                    "ok": True,
                    "delivery_id": str(result.id) if result else None,
                    "status": result.status if result else "NOT_REQUESTED",
                    "remote_id": result.remote_id if result else None,
                    "url": result.url if result else None,
                }
            if name == "plan.propose":
                if scope.role != "console":
                    raise DeliveryError("ROLE_DENIED", "执行角色不能绕过 Console 方案审批")
                raw = cast(dict[str, object], arguments["scope"])
                plan_scope = PlanScope(
                    cast(str, raw["repository_id"]),
                    cast(str, raw["base_revision"]),
                    tuple(cast(list[str], raw["paths"])),
                    tuple(tuple(command) for command in cast(list[list[str]], raw["commands"])),
                    tuple(cast(list[str], raw["actions"])),
                    cast(str | None, raw.get("remote_id")),
                    cast(str | None, raw.get("target_branch")),
                    source_delivery_id=cast(str | None, raw.get("source_delivery_id")),
                )
                plan = await service.propose(
                    scope.actor, scope.run_id, plan_scope, cast(str, arguments["summary"]), cast(str, arguments["risk"])
                )
                return {"ok": True, "plan_id": str(plan.id), "version": plan.version, "status": plan.status}
            if scope.role != "sandbox":
                raise DeliveryError("ROLE_DENIED", "必须经平台批准后切换到执行角色")
            if name == "delivery.review.create":
                review = await service.create_review(
                    scope.actor,
                    scope.run_id,
                    cast(str, arguments["title"]),
                    cast(str, arguments["body"]),
                    scope.fencing_token,
                )
                return {
                    "ok": True,
                    "delivery_id": str(review.id),
                    "status": review.status,
                    "remote_id": review.remote_id,
                    "url": review.url,
                    "kind": "review",
                    "draft": False,
                }
            if name == "delivery.branch.push":
                pushed = await service.publish_branch(
                    scope.actor, scope.run_id, cast(str, arguments["title"]), scope.fencing_token
                )
                return {
                    "ok": True,
                    "delivery_id": str(pushed.id),
                    "status": pushed.status,
                    "remote_id": pushed.remote_id,
                    "url": pushed.url,
                    "kind": "branch_push",
                }
            screenshot = None
            if name == "sandbox.command":
                artifact = await service.command(
                    scope.actor, scope.run_id, tuple(cast(list[str], arguments["argv"])), scope.fencing_token
                )
            elif name == "sandbox.file.write":
                artifact = await service.write(
                    scope.actor,
                    scope.run_id,
                    cast(str, arguments["path"]),
                    cast(str, arguments["content"]),
                    scope.fencing_token,
                )
            elif name == "sandbox.diff":
                artifact = await service.diff(scope.actor, scope.run_id, scope.fencing_token)
            elif name == "browser.verify":
                verification = await service.verify_browser(
                    scope.actor,
                    scope.run_id,
                    cast(str, arguments["path"]),
                    cast(str, arguments["expected_text"]),
                    scope.fencing_token,
                )
                artifact, screenshot = verification.verification, verification.screenshot
            else:
                result = await service.create_pr(
                    scope.actor,
                    scope.run_id,
                    cast(str, arguments["title"]),
                    cast(str, arguments["body"]),
                    scope.fencing_token,
                )
                return {
                    "ok": True,
                    "delivery_id": str(result.id),
                    "status": result.status,
                    "remote_id": result.remote_id,
                    "url": result.url,
                }
            return _artifact_result(artifact, screenshot)
        except DeliveryError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message}}

    for definition in definitions:

        async def handler(
            scope: ToolScope, arguments: dict[str, object], *, name: str = definition.name
        ) -> dict[str, object]:
            return await execute(scope, name, arguments)

        gateway.register(definition, handler)
