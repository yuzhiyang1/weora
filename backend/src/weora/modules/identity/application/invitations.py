"""工作空间邀请的状态与签名格式。"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from weora.modules.identity.application.errors import IdentityError

InviteStatus = Literal["pending", "used", "revoked", "expired"]


class WorkspaceAccessDenied(IdentityError):
    """会话没有当前操作所需的工作空间权限。"""


class InviteUnavailable(IdentityError):
    """无效、已使用、过期或撤销的邀请统一拒绝。"""


class InviteNotConfigured(IdentityError):
    """部署尚未提供邀请链接的稳定签名密钥。"""


class NoActiveWorkspace(IdentityError):
    """凭据正确，但账户需要通过新邀请重新加入工作空间。"""


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: UUID
    name: str
    role: str


@dataclass(frozen=True, slots=True)
class WorkspaceMember:
    id: UUID
    display_name: str
    email: str
    role: str


@dataclass(frozen=True, slots=True)
class WorkspaceInvite:
    id: UUID
    workspace_id: UUID
    workspace_name: str
    creator_name: str
    created_at: datetime
    expires_at: datetime
    used_by: UUID | None
    used_by_name: str | None
    revoked_at: datetime | None

    def status(self, now: datetime) -> InviteStatus:
        """使用和撤销是终态；有效期以服务端 UTC 时间判断。"""
        if self.used_by is not None:
            return "used"
        if self.revoked_at is not None:
            return "revoked"
        expires_at = self.expires_at.replace(tzinfo=UTC) if self.expires_at.tzinfo is None else self.expires_at
        return "expired" if expires_at <= now else "pending"


class InviteSigner:
    """同一有效记录可再次复制，签名不会出现在持久化记录中。"""

    def __init__(self, key: bytes | None) -> None:
        self._key = key

    def sign(self, invite_id: UUID) -> str:
        if self._key is None:
            raise InviteNotConfigured
        signature = hmac.digest(self._key, b"weora-workspace-invite:v1:" + invite_id.bytes, "sha256")
        return f"{invite_id.hex}.{signature.hex()}"

    def verify(self, token: str) -> UUID:
        try:
            invite_id = UUID(hex=token.partition(".")[0])
            # 比较完整的规范形式，避免让别名格式进入签名或审计边界。
            if hmac.compare_digest(self.sign(invite_id).encode("ascii"), token.encode("ascii")):
                return invite_id
        except ValueError:
            pass
        raise InviteUnavailable
