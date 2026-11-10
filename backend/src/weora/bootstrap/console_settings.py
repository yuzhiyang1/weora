"""将团队偏好装配进现有运行端口，不改变任务授权和审批。"""

import re
from dataclasses import replace
from uuid import UUID

from weora.modules.console_settings.domain import ModelConfiguration, SettingsActor, SettingsError
from weora.modules.console_settings.service import ConsoleSettingsService
from weora.modules.identity.application.service import IdentityService
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.harness import OMP_RUNTIME, PI_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile, RunActor


def model_profile(config: ModelConfiguration, base: ExecutionProfile) -> ExecutionProfile:
    """运行快照只携带不透明版本引用，凭据仅在后台解析。"""
    values = config.preferences
    return replace(
        base,
        configuration_id=config.configuration_id,
        provider=values.provider,
        model_id=values.model_id,
        # 新运行交由执行内核按模型和上下文决定输出上限。
        max_tokens=None,
        timeout_seconds=values.timeout_seconds,
        execution_timeout_seconds=values.execution_timeout_seconds,
    )


async def resolve_run_profile(
    service: ConsoleSettingsService,
    workspace_id: UUID,
    frozen: ExecutionProfile | None,
    deployment: ExecutionProfile,
    deployment_key: str | None = None,
) -> tuple[ExecutionProfile, str]:
    """旧运行固定部署配置；有版本引用时绝不静默使用团队最新 Key。"""
    if frozen is None or frozen.runtime_id not in {PI_RUNTIME, OMP_RUNTIME}:
        raise RuntimeFailure("MODEL_CONFIGURATION_MISMATCH")
    if frozen.configuration_id is None:
        # 无版本的历史运行保持部署配置，不被后来保存的控制台设置改变。
        key = service.deployment_key() if deployment_key is None else deployment_key
        expected = deployment
        legacy_max_tokens = deployment.max_tokens
    else:
        try:
            config = await service.resolve_model(workspace_id, frozen.configuration_id)
        except SettingsError as error:
            raise RuntimeFailure(error.code) from None
        key, expected = config.api_key, model_profile(config, deployment)
        legacy_max_tokens = config.preferences.max_tokens
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", frozen.model_id):
        raise RuntimeFailure("MODEL_CONFIGURATION_MISMATCH")
    # 内部协调配置可以为此运行固定其他模型，提供方、预算和凭据仍由平台控制。
    if frozen.runtime_id == OMP_RUNTIME and expected.provider != "deepseek":
        raise RuntimeFailure("MODEL_CONFIGURATION_MISMATCH")
    # 旧快照仍校验当时固定的上限，新快照继续使用执行内核默认值。
    expected = replace(
        expected,
        model_id=frozen.model_id,
        runtime_id=frozen.runtime_id,
        max_tokens=legacy_max_tokens if frozen.max_tokens is not None else None,
    )
    if expected != frozen or not key:
        raise RuntimeFailure("MODEL_CONFIGURATION_MISMATCH" if key else "MODEL_KEY_REQUIRED")
    return expected, key


async def git_identity(
    service: ConsoleSettingsService, identity: IdentityService, actor: RunActor, agent_name: str
) -> dict[str, object]:
    """只在新 Task 冻结署名，后续更新偏好不会改写已获批准任务的提交身份。"""
    principal = await identity.get_active_workspace_principal(user_id=actor.user_id, workspace_id=actor.workspace_id)
    if principal is None:
        raise RuntimeFailure("ACCESS_REVOKED")
    values, revision = await service.git(SettingsActor(actor.user_id, actor.workspace_id))

    def clean(value: str) -> str:
        return re.sub(r"[\r\n\x00<>]", " ", value).strip()[:160]

    agent = clean(agent_name) or "Weora Agent"
    actor_name = clean(principal.display_name) or "Weora"
    name = {
        "manual": values.manual_name,
        "actor_agent": f"{actor_name} & {agent}",
        "actor": actor_name,
        "environment": "Weora",
    }[values.name_mode]
    email = {
        "manual": values.manual_email,
        "actor": principal.email,
        "environment": "weora@localhost",
    }[values.email_mode]
    return {
        "revision": revision,
        "name": clean(name),
        "email": email,
        "co_author": f"{agent} <agents-noreply@weora.local>" if values.co_authored_by else "",
    }
