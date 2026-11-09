"""Identity HTTP 请求与响应契约。"""

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from weora.modules.identity.application.ports import PrincipalRecord


class RegisterRequest(BaseModel):
    """自主注册请求。"""

    display_name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=15, max_length=128)
    workspace_name: str = Field(min_length=2, max_length=120)

    @field_validator("display_name", "workspace_name")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """避免只包含空白字符的名称绕过长度校验。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("不能为空")
        return normalized


class LoginRequest(BaseModel):
    """邮箱密码登录请求。"""

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class PrincipalUserResponse(BaseModel):
    """当前登录用户。"""

    id: UUID
    display_name: str
    email: EmailStr
    avatar_url: str | None = None


class PrincipalWorkspaceResponse(BaseModel):
    """当前请求的工作空间身份。"""

    id: UUID
    name: str
    role: str


class PrincipalResponse(BaseModel):
    """认证成功后的稳定前端契约。"""

    user: PrincipalUserResponse
    workspace: PrincipalWorkspaceResponse

    @classmethod
    def from_record(cls, record: PrincipalRecord) -> "PrincipalResponse":
        """隔离应用层记录与 HTTP 序列化结构。"""
        return cls(
            user=PrincipalUserResponse(
                id=record.user_id,
                display_name=record.display_name,
                email=record.email,
                avatar_url=f"/api/v1/auth/avatar?v={record.avatar_revision}" if record.avatar_revision else None,
            ),
            workspace=PrincipalWorkspaceResponse(
                id=record.workspace_id,
                name=record.workspace_name,
                role=record.role,
            ),
        )


class ErrorResponse(BaseModel):
    """前端可稳定处理的错误载荷。"""

    code: str
    message: str
