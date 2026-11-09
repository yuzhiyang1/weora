"""对象存储 Port 对外暴露的稳定错误。"""

from weora.shared.object_storage.models import ObjectKey


class ObjectStorageError(Exception):
    """对象存储操作失败的基类。"""


class ObjectNotFoundError(ObjectStorageError):
    """指定逻辑对象键不存在。"""

    def __init__(self, key: ObjectKey) -> None:
        super().__init__(f"对象不存在：{key}")
        self.key = key


class ObjectConflictError(ObjectStorageError):
    """同一逻辑对象键已经绑定到不同内容。"""

    def __init__(self, key: ObjectKey) -> None:
        super().__init__(f"对象键已被不同内容占用：{key}")
        self.key = key


class ObjectIntegrityError(ObjectStorageError):
    """对象内容与应用层确认的摘要或长度不一致。"""

    def __init__(self, key: ObjectKey) -> None:
        super().__init__(f"对象完整性校验失败：{key}")
        self.key = key


class ObjectAccessDeniedError(ObjectStorageError):
    """当前凭据无权访问指定对象或 Bucket。"""

    def __init__(self, key: ObjectKey | None = None) -> None:
        message = f"对象访问被拒绝：{key}" if key is not None else "对象存储访问被拒绝"
        super().__init__(message)
        self.key = key


class ObjectStorageTimeoutError(ObjectStorageError):
    """供应商请求在有界重试后仍然超时。"""


class ObjectStorageUnavailableError(ObjectStorageError):
    """网络或供应商服务暂时不可用。"""


class ObjectStorageConfigurationError(ObjectStorageError):
    """对象存储启动配置无效或尚未支持。"""
