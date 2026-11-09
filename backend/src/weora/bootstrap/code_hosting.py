"""代码托管连接配置与装配；只在此选择供应商，不把分支判断扩散到业务层。"""

import re
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from weora.modules.delivery.application.code_hosting import RepositoryConnection


class HostingConnectionSettings(BaseModel):
    """部署级连接白名单。版本或目标变化会产生新指纹，不读取宿主 Git 配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    workspace_id: UUID
    remote_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    version: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    provider: Literal["codeup"]
    repository_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    platform_repository: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    clone_url: str
    api_origin: str
    web_url: str
    credential_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    target_branches: tuple[str, ...] = Field(min_length=1, max_length=20)
    organization: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,100}$")

    @model_validator(mode="after")
    def validate_addresses(self) -> Self:
        for value in (self.clone_url, self.api_origin, self.web_url):
            url = urlsplit(value)
            if (
                url.scheme not in {"https", "ssh"}
                or not url.hostname
                or url.password
                or url.query
                or url.fragment
                or any(char.isspace() or ord(char) < 32 for char in value)
                or "%" in url.netloc
            ):
                raise ValueError("连接地址必须是无凭据、查询参数与片段的登记 HTTPS/SSH 地址")
        clone = urlsplit(self.clone_url)
        if (clone.scheme == "https" and clone.username) or (clone.scheme == "ssh" and clone.username != "git"):
            raise ValueError("HTTPS 地址不能含用户信息，SSH 仅允许显式 git 用户")
        for value in (self.api_origin, self.web_url):
            url = urlsplit(value)
            if url.scheme != "https" or url.username:
                raise ValueError("API 和评审页面必须使用无认证信息的 HTTPS 地址")
        if urlsplit(self.api_origin).path:
            raise ValueError("API 地址只能登记 origin，不含路径或尾部斜杠")
        for branch in self.target_branches:
            if (
                not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", branch)
                or ".." in branch
                or branch.startswith(("/", "-"))
                or branch.endswith(("/", ".", ".lock"))
                or "//" in branch
            ):
                raise ValueError("目标分支格式错误")
        return self

    def connection(self) -> RepositoryConnection:
        return RepositoryConnection(**self.model_dump())
