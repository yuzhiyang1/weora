"""云效新版 OAPI 的只读评审核对；没有文档化草稿创建参数时不降级正式评审。"""

import json
from typing import cast
from urllib.parse import quote, urlsplit

import aiohttp

from weora.modules.delivery.application.code_hosting import (
    PublishedBranch,
    RepositoryConnection,
    ReviewCapabilities,
    delivery_branch,
)
from weora.modules.delivery.application.ports import RemoteResult
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.git import Credentials


def verified_review_result(connection: RepositoryConnection, row: dict[str, object]) -> RemoteResult:
    """云效 webUrl 可以是仓库首页，只有被精确校验的 detailUrl 才能作为 MR 直达链接。"""
    local_id = row.get("localId")
    if type(local_id) is not int or local_id <= 0:
        raise DeliveryError("REMOTE_PROTOCOL", "云效未返回合法评审标识")
    repository_url = connection.web_url.rstrip("/")
    expected = repository_url + "/change/" + str(local_id)
    detail, web = row.get("detailUrl"), row.get("webUrl")
    if not ((detail == expected and web in (None, repository_url, expected)) or (detail is None and web == expected)):
        raise DeliveryError("REMOTE_PROTOCOL", "云效评审地址与登记仓库不匹配")
    return RemoteResult(str(local_id), expected)


class CodeupReviews:
    """支持中心版/Region 版只读分页；固定 origin 与路径，不接受模型 URL。"""

    capabilities = ReviewCapabilities(draft=False)

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials

    async def create_draft(
        self,
        connection: RepositoryConnection,
        branch: PublishedBranch,
        scope: PlanScope,
        business_key: str,
        title: str,
        body: str,
    ) -> RemoteResult:
        raise DeliveryError("CAPABILITY_UNSUPPORTED", "云效新版 OAPI 草稿创建尚未验证，不创建正式评审")

    async def _page(self, connection: RepositoryConnection, page: int) -> tuple[list[dict[str, object]], int | None]:
        origin = urlsplit(connection.api_origin)
        if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path:
            raise DeliveryError("REMOTE_DENIED", "云效 API 必须使用登记的 HTTPS origin")
        prefix = "/oapi/v1/codeup"
        if connection.organization:
            prefix += "/organizations/" + quote(connection.organization, safe="")
        token = self.credentials.get(connection.credential_ref).api_token
        if not token or any(char in token for char in "\r\n"):
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "云效 API 凭据引用不可用")
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session,
                session.get(
                    connection.api_origin + prefix + "/changeRequests",
                    params={"projectIds": connection.platform_repository, "page": str(page), "perPage": "100"},
                    headers={"x-yunxiao-token": token},
                    allow_redirects=False,
                ) as response,
            ):
                if response.status >= 500:
                    raise ConnectionError("REMOTE_UNKNOWN")
                if response.status >= 300:
                    raise DeliveryError("REMOTE_REJECTED", "云效拒绝只读评审核对")
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise DeliveryError("REMOTE_PROTOCOL", "云效响应超过限制")
                rows = json.loads(raw)
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    raise ValueError
                next_page = response.headers.get("x-next-page", "")
                # 云效真实末页会把 next 回填当前页，不能只靠 next 猜测结束。
                # 总页数、当前页与行数一致时才认作末页；没有这些证据仍拒绝重复页。
                total_pages = response.headers.get("x-total-pages", "")
                total = response.headers.get("x-total", "")
                current_page = response.headers.get("x-page", "")
                if total_pages:
                    if not total_pages.isdecimal() or (current_page and current_page != str(page)):
                        raise ValueError
                    if int(total_pages) == page and next_page == str(page):
                        if not total.isdecimal() or int(total) != (page - 1) * 100 + len(rows):
                            raise ValueError
                        return cast(list[dict[str, object]], rows), None
                if len(rows) >= 100 and "x-next-page" not in response.headers:
                    raise DeliveryError("RECONCILIATION_INCOMPLETE", "满页响应缺少分页信息，不能判定核对完整")
                if next_page and (not next_page.isdecimal() or int(next_page) != page + 1):
                    raise ValueError
                return cast(list[dict[str, object]], rows), int(next_page) if next_page else None
        except aiohttp.ClientError:
            raise ConnectionError("REMOTE_UNKNOWN") from None
        except (ValueError, TypeError):
            raise DeliveryError("REMOTE_PROTOCOL", "云效响应结构无效") from None

    async def reconcile(
        self,
        connection: RepositoryConnection,
        scope: PlanScope,
        business_key: str,
    ) -> RemoteResult | None:
        if connection.provider != "codeup" or scope.remote_binding != connection.fingerprint:
            raise DeliveryError("REMOTE_BINDING_CHANGED", "原云效连接不匹配")
        branch, marker = delivery_branch(business_key), "<!-- weora-delivery:" + business_key + " -->"
        matches: list[RemoteResult] = []
        page: int | None = 1
        for _ in range(5):
            assert page is not None
            rows, page = await self._page(connection, page)
            for row in rows:
                if (
                    row.get("sourceBranch") != branch
                    or row.get("targetBranch") != scope.target_branch
                    or str(row.get("sourceProjectId")) != connection.platform_repository
                    or str(row.get("targetProjectId")) != connection.platform_repository
                    or marker not in str(row.get("description", ""))
                ):
                    continue
                # 正式评审不能被认作本功能承诺的草稿；标题里的 WIP/Draft 也不是证据。
                if row.get("workInProgress") is not True and row.get("state") != "UNDER_DEV":
                    continue
                matches.append(verified_review_result(connection, row))
            if page is None:
                if len(matches) > 1:
                    raise DeliveryError("REMOTE_AMBIGUOUS", "业务键对应多条评审，需要人工核对")
                return matches[0] if matches else None
        raise DeliveryError("RECONCILIATION_INCOMPLETE", "评审核对超过分页预算，不能断言远端不存在")
