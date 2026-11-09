"""从可信部署配置生成无凭据的固定运行配置。"""

from weora.bootstrap.settings import AppSettings
from weora.modules.runs.application.harness import PI_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile


def execution_profile(settings: AppSettings) -> ExecutionProfile | None:
    """每个新 Run 保存快照；更改模型配置不会悄悄改变已有运行。"""
    if settings.run_runtime == "disabled":
        return None
    return ExecutionProfile(
        runtime_id=PI_RUNTIME,
        provider="deepseek",
        model_id=settings.pi_model or "",
        max_tokens=settings.run_model_max_tokens,
        timeout_seconds=settings.run_model_timeout_seconds,
        execution_timeout_seconds=settings.run_execution_timeout_seconds,
    )
