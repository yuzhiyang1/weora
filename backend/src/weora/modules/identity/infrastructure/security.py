"""密码摘要和会话令牌的安全实现。"""

import hashlib
import secrets

from pwdlib import PasswordHash


class Argon2PasswordHasher:
    """使用 pwdlib 推荐参数的 Argon2id 密码摘要器。"""

    def __init__(self) -> None:
        self._password_hash = PasswordHash.recommended()
        # 未知邮箱仍执行一次同成本校验，降低账户枚举的时序差异。
        self._dummy_hash = self._password_hash.hash("weora-dummy-password-never-used")

    def hash(self, password: str) -> str:
        """生成带随机盐的密码摘要。"""
        return self._password_hash.hash(password)

    def verify(self, password: str, password_hash: str | None) -> bool:
        """以常量时间实现校验已有密码摘要。"""
        return self._password_hash.verify(password, password_hash or self._dummy_hash)


class SecureTokenManager:
    """签发只在浏览器出现的高熵令牌，数据库仅保存 SHA-256 摘要。"""

    def issue(self) -> str:
        """签发至少 256 bit 随机性的 URL 安全令牌。"""
        return secrets.token_urlsafe(32)

    def digest(self, token: str) -> str:
        """高熵令牌可以安全使用快速摘要完成索引查询。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
