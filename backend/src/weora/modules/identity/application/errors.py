"""Identity 应用层对外暴露的稳定错误。"""


class IdentityError(Exception):
    """Identity 用例失败的基类。"""


class EmailUnavailableError(IdentityError):
    """邮箱不可用于创建新账户。"""


class AuthenticationRequiredError(IdentityError):
    """请求没有携带有效会话。"""


class RegistrationDisabledError(IdentityError):
    """当前部署不允许自主注册。"""


class InvalidCredentialsError(IdentityError):
    """登录凭据不正确或账户不可用。"""


class CsrfRejectedError(IdentityError):
    """状态变更请求没有提供与会话绑定的 CSRF 令牌。"""


class LoginRateLimitedError(IdentityError):
    """短时间内错误凭据次数超过安全阈值。"""
