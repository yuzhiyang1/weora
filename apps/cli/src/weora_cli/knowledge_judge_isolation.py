"""评分客户端的隔离级别和版本核验。"""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from weora_cli.collab_client import CollabError

type Isolation = Literal["best-effort", "verified"]

# 版本核验参考 TeamAI 的受限配置。新增版本需要验证其工具及配置禁用行为。
_PROFILES = {"codex": "codex-cli 0.163.0", "claude": "2.1.308 (Claude Code)"}
_COMMON = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR",
    "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
}
_PROVIDER = {
    "codex": {"CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"},
    "claude": {
        "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    },
}


def isolation_level(value: object) -> Isolation | None:
    if value is None or value == "":
        return None
    # 未识别级别按 TeamAI 的规则提升为 verified。
    return "best-effort" if value == "best-effort" else "verified"


def judge_isolation(*requirements: object) -> Isolation | None:
    levels = {isolation_level(value) for value in (*requirements, os.environ.get("WEORA_SCORING_ISOLATION"))}
    return "verified" if "verified" in levels else "best-effort" if "best-effort" in levels else None


def isolated_environment(driver: str) -> dict[str, str]:
    allowed = _COMMON | _PROVIDER.get(driver, set())
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def require_profile(driver: str, executable: str, directory: Path, env: dict[str, str]) -> None:
    expected = _PROFILES.get(driver)
    try:
        probe = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, cwd=directory, env=env, timeout=5, check=False,
        )
        if expected is not None and probe.returncode == 0 and probe.stdout.strip() == expected:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise CollabError(
        "KNOWLEDGE_SCORING_ISOLATION",
        f"{driver} 当前无法达到 verified 评分隔离级别。已核验版本为 {expected or '无'}。",
    )


def verified_clients() -> list[str]:
    """领取前只核验版本。收到执行计划后再核验所选客户端。"""
    verified: list[str] = []
    with TemporaryDirectory(prefix="weora-judge-probe-") as scratch:
        for driver in _PROFILES:
            executable = shutil.which(driver)
            if executable is None:
                continue
            try:
                require_profile(driver, os.path.abspath(executable), Path(scratch), isolated_environment(driver))
            except CollabError:
                continue
            verified.append(driver)
    return verified
