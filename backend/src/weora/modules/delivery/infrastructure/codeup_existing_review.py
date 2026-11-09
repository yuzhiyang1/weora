"""普通 MR 适配器：只消费已发布分支，不绕过原草稿适配器的能力门槛。"""

import json
from urllib.parse import quote, urlsplit

import aiohttp

from weora.modules.delivery.application.code_hosting import PublishedBranch, RepositoryConnection
from weora.modules.delivery.application.ports import RemoteResult
from weora.modules.delivery.domain.models import DeliveryError, PlanScope, ReviewNotSent
from weora.modules.delivery.infrastructure.codeup import CodeupReviews, verified_review_result


class CodeupExistingReviews(CodeupReviews):
    async def verify_repository(self, connection: RepositoryConnection) -> None:
        """自助登记的数字 ID 必须经远端只读映射，不能仅凭用户填写就向另一个仓库 POST。"""
        prefix = "/oapi/v1/codeup"
        if connection.organization:
            prefix += "/organizations/" + quote(connection.organization, safe="")
        token = self.credentials.get(connection.credential_ref).api_token
        if not token or any(char.isspace() for char in token):
            raise ReviewNotSent("CREDENTIAL_UNAVAILABLE", "MR API 凭据不可用，尚未发送创建请求")
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session,
                session.get(
                    connection.api_origin + prefix + "/repositories/" + quote(connection.platform_repository, safe=""),
                    headers={"x-yunxiao-token": token},
                    allow_redirects=False,
                ) as response,
            ):
                if response.status != 200:
                    raise ReviewNotSent("REPOSITORY_API_UNVERIFIED", "无法核对 MR API 仓库身份，尚未发送创建请求")
                raw = await response.content.read(512 * 1024 + 1)
                if len(raw) > 512 * 1024:
                    raise ValueError
                row = json.loads(raw)
                if (
                    not isinstance(row, dict)
                    or str(row.get("id")) != connection.platform_repository
                    or row.get("httpUrlToRepo") != connection.clone_url
                    or row.get("webUrl") != connection.web_url
                ):
                    raise ReviewNotSent("REPOSITORY_API_MISMATCH", "平台仓库 ID 与 Git 地址不匹配，尚未发送创建请求")
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            raise ReviewNotSent("REPOSITORY_API_UNVERIFIED", "仓库只读核对未完成，尚未发送创建请求") from None

    @staticmethod
    def _result(
        connection: RepositoryConnection, scope: PlanScope, branch: PublishedBranch, key: str, row: dict[str, object]
    ) -> RemoteResult | None:
        marker = "<!-- weora-delivery:" + key + " -->"
        if (
            row.get("sourceBranch") != branch.name
            or row.get("targetBranch") != scope.target_branch
            or str(row.get("sourceProjectId")) != connection.platform_repository
            or str(row.get("targetProjectId")) != connection.platform_repository
            or marker not in str(row.get("description", ""))
        ):
            return None
        # 创建/详情使用 status，列表使用 state；冲突时不能选择性相信其中之一。
        state = row.get("status", row.get("state"))
        if row.get("status") is not None and row.get("state") is not None and row["status"] != row["state"]:
            raise DeliveryError("REMOTE_PROTOCOL", "云效 MR 状态字段互相冲突")
        if state not in {"UNDER_DEV", "UNDER_REVIEW", "TO_BE_MERGED"}:
            raise DeliveryError("REMOTE_PROTOCOL", "云效 MR 未合并状态不符合预期")
        return verified_review_result(connection, row)

    async def reconcile_existing(
        self, connection: RepositoryConnection, scope: PlanScope, branch: PublishedBranch, key: str
    ) -> RemoteResult | None:
        if connection.provider != "codeup" or scope.remote_binding != connection.fingerprint:
            raise DeliveryError("REMOTE_BINDING_CHANGED", "原普通 MR 连接已变化")
        matches: list[RemoteResult] = []
        page: int | None = 1
        for _ in range(5):
            assert page is not None
            rows, page = await self._page(connection, page)
            for row in rows:
                result = self._result(connection, scope, branch, key, row)
                if result:
                    matches.append(result)
            if page is None:
                if len(matches) > 1:
                    raise DeliveryError("REMOTE_AMBIGUOUS", "相同业务键对应多条 MR，需人工核对")
                return matches[0] if matches else None
        raise DeliveryError("RECONCILIATION_INCOMPLETE", "MR 核对超过分页预算，不能视为不存在")

    async def create_existing(
        self,
        connection: RepositoryConnection,
        scope: PlanScope,
        branch: PublishedBranch,
        key: str,
        title: str,
        body: str,
    ) -> RemoteResult:
        """单次 POST，无隐式网络重试；原有 MR 用只读核对复用，不重新发布 Git。"""
        try:
            found = await self.reconcile_existing(connection, scope, branch, key)
        except DeliveryError as error:
            raise ReviewNotSent(error.code, "只读预检未通过，尚未发送 MR 创建请求") from None
        except (ConnectionError, TimeoutError):
            raise ReviewNotSent("REVIEW_PREFLIGHT_FAILED", "只读预检未完成，尚未发送 MR 创建请求") from None
        if found:
            return found
        origin = urlsplit(connection.api_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise DeliveryError("REMOTE_DENIED", "MR API 必须使用登记的 HTTPS origin")
        token = self.credentials.get(connection.credential_ref).api_token
        if not token or any(char.isspace() for char in token):
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "MR API 令牌不可用")
        prefix = "/oapi/v1/codeup"
        if connection.organization:
            prefix += "/organizations/" + quote(connection.organization, safe="")
        path = prefix + "/repositories/" + quote(connection.platform_repository, safe="") + "/changeRequests"
        payload = {
            "sourceBranch": branch.name,
            "targetBranch": scope.target_branch,
            "sourceProjectId": int(connection.platform_repository),
            "targetProjectId": int(connection.platform_repository),
            "title": title,
            "description": body + "\n\n<!-- weora-delivery:" + key + " -->",
            "triggerAIReviewRun": False,
        }
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session,
                session.post(
                    connection.api_origin + path,
                    json=payload,
                    headers={"x-yunxiao-token": token},
                    allow_redirects=False,
                ) as response,
            ):
                if response.status >= 500:
                    raise ConnectionError("REMOTE_UNKNOWN")
                if response.status not in {200, 201}:
                    raise DeliveryError("REMOTE_REJECTED", "云效拒绝创建普通 MR")
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise DeliveryError("REMOTE_PROTOCOL", "MR 响应超过限制")
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError
                result = self._result(connection, scope, branch, key, row)
                if result is None:
                    raise DeliveryError("REMOTE_PROTOCOL", "MR 响应与批准的分支不匹配")
                return result
        except aiohttp.ClientError:
            raise ConnectionError("REMOTE_UNKNOWN") from None
        except (ValueError, TypeError):
            raise DeliveryError("REMOTE_PROTOCOL", "MR 响应结构无效") from None
