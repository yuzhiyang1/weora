"""跨机协作的公共契约和投递时限。"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

PAIRING_TTL = timedelta(minutes=10)
DEVICE_TTL = timedelta(days=30)
PRESENCE_TTL = timedelta(seconds=45)
PENDING_TTL = timedelta(days=1)
CLAIM_TTL = timedelta(minutes=30)
WAKE_TTL = timedelta(minutes=2)
LOOP_WINDOW = timedelta(minutes=5)
LOOP_LIMIT = 12


class AgentMailError(Exception):
    """可安全反馈给调用方的协作错误。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def unavailable() -> AgentMailError:
    return AgentMailError(404, "AGENT_COLLABORATION_UNAVAILABLE", "目标不存在或无访问权限")


def conflict() -> AgentMailError:
    return AgentMailError(409, "AGENT_COLLABORATION_CONFLICT", "请求标识已用于不同操作，或消息状态已变化")


@dataclass(frozen=True)
class SessionRegistration:
    """只交换显式登记的目录信息，原生会话正文和绝对路径留在设备。"""

    runtime: str
    profile: str
    native_id: str
    name: str
    repo: str
    branch: str
    delivery: str
    preserve_name: bool = False


@dataclass(frozen=True)
class SendRequest:
    """客户端稳定的发送键支持连接中断后的重试。"""

    sender_id: UUID
    recipient_id: UUID
    key: str
    body: str
    task_id: UUID | None = None
    reply_to: UUID | None = None
    expects_reply: bool = True
    wake: bool = True
