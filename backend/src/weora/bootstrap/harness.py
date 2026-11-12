"""将已冻结的运行配置映射到部署管理员登记的进程入口。"""

import shutil
from pathlib import Path

from weora.bootstrap.settings import AppSettings
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.harness import OMP_RUNTIME, PI_RUNTIME, HarnessFactory
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.pi_runtime import PiTextRuntime
from weora.modules.runs.infrastructure.process_runtime import ProcessRuntime


def omp_configured(settings: AppSettings) -> bool:
    """配置状态仅检查部署入口，模型联通性由实际运行验证。"""
    return bool(
        settings.run_runtime != "disabled"
        and settings.omp_runtime_entry
        and Path(settings.omp_runtime_entry).is_file()
        and shutil.which(settings.omp_bun_executable)
    )


def harness_factory(settings: AppSettings, profile: ExecutionProfile, api_key: str) -> HarnessFactory:
    if profile.runtime_id == PI_RUNTIME:
        return lambda session, call: PiTextRuntime(
            node_executable=settings.pi_node_executable,
            entry=Path(settings.pi_runtime_entry or ""),
            api_key=api_key,
            call_tool=call,
            session=session,
        )
    if profile.runtime_id == OMP_RUNTIME and profile.provider == "deepseek" and omp_configured(settings):
        return lambda session, call: ProcessRuntime(
            executable=settings.omp_bun_executable,
            runtime_id=OMP_RUNTIME,
            providers=frozenset({"deepseek"}),
            entry=Path(settings.omp_runtime_entry or ""),
            private_home=True,
            report_ready=True,
            api_key=api_key,
            call_tool=call,
            session=session,
        )
    raise RuntimeFailure("RUNTIME_NOT_CONFIGURED")
