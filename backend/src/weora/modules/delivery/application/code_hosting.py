"""代码托管端口与组合策略；平台负责路由，模型只能引用登记的远端。"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from weora.modules.delivery.application.ports import DraftPullRequests, RemoteResult
from weora.modules.delivery.domain.models import DeliveryError, PlanScope


@dataclass(frozen=True)
class RepositoryConnection:
    """管理员登记的不可变连接；凭据只保存引用，不保存秘密或从 URL 推断平台。"""

    workspace_id: UUID
    remote_id: str
    version: str
    provider: str
    repository_id: str
    platform_repository: str
    clone_url: str
    api_origin: str
    web_url: str
    credential_ref: str
    target_branches: tuple[str, ...]
    organization: str = ""

    @property
    def fingerprint(self) -> str:
        """包括地址、凭据引用版本与工作空间；迁移平台后旧批准不能漂移到新目标。"""
        values = asdict(self) | {"workspace_id": str(self.workspace_id)}
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ReviewCapabilities:
    draft: bool
    read_reconcile: bool = True


@dataclass(frozen=True)
class PublishedBranch:
    name: str
    revision: str


def delivery_branch(business_key: str) -> str:
    """分支名只来自平台业务键，不能由模型提供或覆盖目标分支。"""
    if len(business_key) != 64 or any(char not in "0123456789abcdef" for char in business_key):
        raise DeliveryError("INVALID_DELIVERY_KEY", "交付业务键格式错误")
    return "weora/" + business_key[:24]


class GitPublisher(Protocol):
    async def publish(
        self,
        run_id: UUID,
        scope: PlanScope,
        connection: RepositoryConnection,
        business_key: str,
        title: str,
    ) -> PublishedBranch:
        """仅发布获批 Diff 到业务键专用分支，不创建平台评审或合并。"""
        ...


class CodeReviewProvider(Protocol):
    capabilities: ReviewCapabilities

    async def create_draft(
        self,
        connection: RepositoryConnection,
        branch: PublishedBranch,
        scope: PlanScope,
        business_key: str,
        title: str,
        body: str,
    ) -> RemoteResult: ...

    async def reconcile(
        self,
        connection: RepositoryConnection,
        scope: PlanScope,
        business_key: str,
    ) -> RemoteResult | None:
        """只读查回；查不到不代表创建失败，也不能触发再次推送或创建。"""
        ...


class ConnectionBindings(Protocol):
    def freeze(self, workspace_id: UUID, scope: PlanScope) -> PlanScope:
        """由平台冻结目标，不接受模型提供连接指纹。"""
        ...


class RepositoryDelivery:
    """组合 Git 与评审策略；在任何 Git 副作用前检查供应商能力。"""

    def __init__(self, connection: RepositoryConnection, git: GitPublisher, review: CodeReviewProvider) -> None:
        self.connection, self.git, self.review = connection, git, review

    async def create(
        self,
        run_id: UUID,
        scope: PlanScope,
        business_key: str,
        title: str,
        body: str,
    ) -> RemoteResult:
        if not self.review.capabilities.draft:
            raise DeliveryError("CAPABILITY_UNSUPPORTED", "该平台适配器尚未验证草稿能力，不会推送或创建正式评审")
        branch = await self.git.publish(run_id, scope, self.connection, business_key, title)
        result = await self.review.create_draft(self.connection, branch, scope, business_key, title, body)
        return replace(result, branch_name=branch.name, branch_revision=branch.revision)

    async def reconcile(self, scope: PlanScope, business_key: str) -> RemoteResult | None:
        if not self.review.capabilities.read_reconcile:
            raise DeliveryError("CAPABILITY_UNSUPPORTED", "该平台不支持只读对账")
        return await self.review.reconcile(self.connection, scope, business_key)


class RepositoryRegistry:
    """每仓库选择策略；按工作空间登记，执行和恢复均核对原计划的连接指纹。"""

    def __init__(self, entries: Mapping[RepositoryConnection, DraftPullRequests]) -> None:
        self.entries = MappingProxyType(dict(entries))
        identities = [(item.workspace_id, item.remote_id) for item in entries]
        if len(set(identities)) != len(identities):
            raise ValueError("同一工作空间不能登记重复的远端 ID")

    @staticmethod
    def _validate_scope(connection: RepositoryConnection, scope: PlanScope) -> None:
        if scope.repository_id != connection.repository_id or scope.target_branch not in connection.target_branches:
            raise DeliveryError("REMOTE_DENIED", "仓库或目标分支不属于登记的远端")

    def freeze(self, workspace_id: UUID, scope: PlanScope) -> PlanScope:
        if not {"git.push", "pr.create"} & set(scope.actions):
            return scope
        for connection in self.entries:
            if (connection.workspace_id, connection.remote_id) == (workspace_id, scope.remote_id):
                self._validate_scope(connection, scope)
                return replace(scope, remote_binding=connection.fingerprint)
        raise DeliveryError("REMOTE_DENIED", "该工作空间未登记远端连接")

    def _resolve(self, scope: PlanScope) -> DraftPullRequests:
        for connection, adapter in self.entries.items():
            if scope.remote_id == connection.remote_id and scope.remote_binding == connection.fingerprint:
                self._validate_scope(connection, scope)
                return adapter
        # 不能把无绑定的旧计划自动指向当前同名远端；需要新计划和重新批准。
        raise DeliveryError("REMOTE_BINDING_CHANGED", "原远端连接已变化或未冻结，请核对原连接；不会改用新平台")

    async def create(
        self,
        run_id: UUID,
        scope: PlanScope,
        business_key: str,
        title: str,
        body: str,
    ) -> RemoteResult:
        return await self._resolve(scope).create(run_id, scope, business_key, title, body)

    async def reconcile(self, scope: PlanScope, business_key: str) -> RemoteResult | None:
        return await self._resolve(scope).reconcile(scope, business_key)
