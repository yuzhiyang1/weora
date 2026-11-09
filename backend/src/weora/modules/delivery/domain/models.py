"""平台拥有执行授权；模型提出计划，不拥有审批能力。"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

Decision = Literal["approve", "reject", "supplement", "revoke"]
PlanStatus = Literal["PENDING", "REVISION_REQUIRED", "APPROVED", "REJECTED", "SUPERSEDED", "REVOKED"]

# 领域校验与模型可见 schema 共用同一组权限名，避免工具名和审批动作混淆。
PLAN_ACTIONS = (
    "file.write",
    "command.run",
    "git.diff",
    "browser.verify",
    "git.commit",
    "git.push",
    "pr.create",
    "review.create",
)


class DeliveryError(Exception):
    """只暴露稳定错误码，不把供应商响应或凭据传给客户端。"""

    def __init__(self, code: str, message: str = "研发交付操作未完成") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ReviewNotSent(DeliveryError):
    """适配器确定尚未进入 POST；只允许新计划人工审批后再尝试，不能自动重试。"""


def safe_path(value: str) -> str:
    """所有模型路径采用相对 POSIX 形式；不接受 Windows、URL 或隐藏 Git 元数据。"""
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 512
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"..", ".git"} for part in path.parts)
        or str(path) != value
        or value.startswith("-")
    ):
        raise DeliveryError("INVALID_PATH", "路径必须是仓库内的相对路径，且不能访问 Git 元数据")
    return value


@dataclass(frozen=True)
class PlanScope:
    """人工批准的精确范围；命令按 argv 精确匹配，远端操作必须单列。"""

    repository_id: str
    base_revision: str
    paths: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]
    actions: tuple[str, ...]
    remote_id: str | None = None
    target_branch: str | None = None
    # 平台在提案时注入，模型/API 不能提交；绑定确切仓库、实例与连接版本。
    remote_binding: str | None = None
    # 已成功分支交付的不可变记录 ID；普通 MR 只引用它，不重新生成提交或推送。
    source_delivery_id: str | None = None
    # 平台从已成功交付记录注入；新审批精确展示要续写的专用分支及预期提交。
    source_branch: str | None = None
    source_revision: str | None = None

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.repository_id):
            raise DeliveryError("INVALID_SCOPE", "仓库标识格式错误")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.base_revision):
            raise DeliveryError("INVALID_SCOPE", "基准必须为固定的完整 revision")
        if not self.paths or len(self.paths) > 100 or len(self.commands) > 30:
            raise DeliveryError("INVALID_SCOPE", "修改范围或命令数量不符合限制")
        for path in self.paths:
            safe_path(path)
        if not self.actions or not set(self.actions) <= set(PLAN_ACTIONS):
            raise DeliveryError("INVALID_SCOPE", "包含未支持的执行动作")
        for command in self.commands:
            if not command or len(command) > 64 or any(not x or len(x) > 2048 or "\x00" in x for x in command):
                raise DeliveryError("INVALID_SCOPE", "命令必须提供有界 argv")
        if "review.create" in self.actions:
            if set(self.actions) != {"review.create"} or self.commands or not self.source_delivery_id:
                raise DeliveryError("INVALID_SCOPE", "普通 MR 计划只能引用已交付分支，不能混入改码或推送")
            try:
                UUID(self.source_delivery_id)
            except ValueError:
                raise DeliveryError("INVALID_SCOPE", "必须提供合法分支交付记录 ID") from None
        elif self.source_delivery_id is not None:
            if not {"git.commit", "git.push"} <= set(self.actions) or "pr.create" in self.actions:
                raise DeliveryError("INVALID_SCOPE", "续写分支必须分别批准提交和推送，不能再创建 PR")
            try:
                UUID(self.source_delivery_id)
            except ValueError:
                raise DeliveryError("INVALID_SCOPE", "必须提供合法分支交付记录 ID") from None
        if (self.source_branch is not None or self.source_revision is not None) and (
            not self.source_delivery_id
            or "git.push" not in self.actions
            or not re.fullmatch(r"weora/[0-9a-f]{24}", self.source_branch or "")
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.source_revision or "")
        ):
            raise DeliveryError("INVALID_SCOPE", "续写分支的冻结元数据不合法")
        if {"git.push", "pr.create", "review.create"} & set(self.actions):
            if not self.remote_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.remote_id):
                raise DeliveryError("INVALID_SCOPE", "推送与 PR 必须指定管理员登记的远端")
            if not self.target_branch or not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", self.target_branch):
                raise DeliveryError("INVALID_SCOPE", "推送与 PR 必须指定目标分支")
            if ".." in self.target_branch or self.target_branch.startswith(("/", "-")):
                raise DeliveryError("INVALID_SCOPE", "目标分支不合法")

    @property
    def digest(self) -> str:
        values = asdict(self)
        # 未绑定的历史计划保持原摘要，避免破坏已经保存的审批和证据关联。
        if self.remote_binding is None:
            values.pop("remote_binding")
        if self.source_delivery_id is None:
            values.pop("source_delivery_id")
        for field in ("source_branch", "source_revision"):
            if values[field] is None:
                values.pop(field)
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def allows_path(self, path: str) -> bool:
        safe_path(path)
        return any(path == prefix or path.startswith(prefix + "/") for prefix in self.paths)


@dataclass(frozen=True)
class PlanRecord:
    id: UUID
    run_id: UUID
    workspace_id: UUID
    created_by: UUID
    version: int
    scope: PlanScope
    summary: str
    risk: str
    status: PlanStatus
    created_at: datetime
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    id: UUID
    run_id: UUID
    workspace_id: UUID
    kind: str
    status: str
    body: str | None
    created_at: datetime
    expires_at: datetime
    sha256: str
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    sequence: int = 0
    plan_id: UUID | None = None
    repository_sha256: str | None = None


@dataclass(frozen=True)
class RemoteRecord:
    """UNKNOWN 必须先对账；不能将未知结果当成可重试失败。"""

    id: UUID
    run_id: UUID
    workspace_id: UUID
    business_key: str
    status: str
    attempts: int
    created_at: datetime
    updated_at: datetime
    remote_id: str | None = None
    url: str | None = None
    error_code: str | None = None
    plan_id: UUID | None = None
