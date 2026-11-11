"""应用启动配置。

配置只描述如何装配应用，不承载具体业务规则。部署环境可通过 ``WEORA_`` 前缀
的环境变量覆盖默认值，例如 ``WEORA_API_PREFIX=/internal/v1``。
"""

from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from weora.bootstrap.code_hosting import HostingConnectionSettings


class ProjectSandboxResources(BaseModel):
    """项目镜像的管理员资源预算；不作为 Agent 工具的输入字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    memory_mb: int = Field(default=512, ge=128, le=4096)
    cpus: float = Field(default=1.0, ge=0.1, le=4)
    pids: int = Field(default=128, ge=16, le=512)
    timeout_seconds: float = Field(default=60, ge=1, le=600)
    output_bytes: int = Field(default=65536, ge=1024, le=262144)
    workspace_mb: int = Field(default=512, ge=64, le=4096)
    temporary_mb: int = Field(default=64, ge=16, le=512)


class AppSettings(BaseSettings):
    """Weora API 的进程级配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="WEORA_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # 内网电脑服务凭据只进入 API 和 Worker，功能缺省关闭。
    computer_supervisor_url: str | None = None
    computer_supervisor_token: SecretStr | None = None
    computer_master_token: SecretStr | None = None
    computer_browser_origins: tuple[str, ...] = ()
    computer_feishu_scopes: dict[str, Literal["observe", "test"]] = Field(default_factory=dict)

    app_name: str = "Weora API"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "postgresql+asyncpg://weora@127.0.0.1:5432/weora"
    registration_enabled: bool = True
    workspace_invite_key: SecretStr | None = None
    session_cookie_name: str = "weora_session"
    csrf_cookie_name: str = "weora_csrf"
    session_cookie_secure: bool = False
    session_ttl_days: int = 7
    development_auth_bypass_enabled: bool = False
    login_max_failures: int = 5
    login_failure_window_minutes: int = 15
    object_storage_provider: Literal["disabled", "aliyun_oss"] = "disabled"
    object_storage_bucket: str | None = None
    object_storage_region: str | None = None
    object_storage_endpoint: str | None = None
    object_storage_access_key_id: SecretStr | None = None
    object_storage_access_key_secret: SecretStr | None = None
    object_storage_security_token: SecretStr | None = None
    object_storage_prefix: str | None = None
    object_storage_use_cname: bool = False
    object_storage_connect_timeout_seconds: float = Field(default=5, gt=0)
    object_storage_read_timeout_seconds: float = Field(default=30, gt=0)
    object_storage_max_attempts: int = Field(default=3, ge=1, le=10)
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:3000",
        ]
    )
    # 只读文本执行默认禁用。生产密钥只在后端持有，不进入 Run 配置快照。
    run_runtime: Literal["disabled", "pi"] = "disabled"
    pi_node_executable: str = "node"
    pi_runtime_entry: str | None = None
    omp_bun_executable: str = "bun"
    omp_runtime_entry: str | None = None
    pi_model: str | None = None
    deepseek_api_key: SecretStr | None = None
    # 多工作空间部署可以仅使用各空间自己保存的模型密钥。
    run_workspace_model_keys_only: bool = False
    # 兼容旧预算 API 和已有运行快照，新运行使用执行内核默认值。
    run_model_max_tokens: int = Field(default=4096, ge=1, le=16384)
    run_model_timeout_seconds: int = Field(default=120, ge=1, le=600)
    # 模型等待与包含克隆、工具的整体执行分别计时，不能让长构建消耗模型预算。
    run_execution_timeout_seconds: int = Field(default=1800, ge=1, le=3600)
    run_lease_seconds: int = Field(default=30, ge=3, le=300)
    celery_broker_url: str = "redis://127.0.0.1:6379/0"
    # 两个部署密钥必须一起配置；不临时生成，否则重启后凭据/游标不可恢复。
    context_encryption_key: SecretStr | None = None
    context_cursor_key: SecretStr | None = None
    context_retention_days: int = Field(default=7, ge=1, le=7)
    # 消息读取可切换 CLI；接收 SDK 与发送 Outbox 不变，默认 HTTP 兼容现有部署。
    feishu_reader_backend: Literal["http", "cli"] = "http"
    feishu_cli_executable: str | None = None
    feishu_cli_timeout_seconds: float = Field(default=2.5, gt=0, le=2.5)
    # Agent 命令使用支持官方 authsidecar 的独立二进制，群消息适配器仍可走 HTTP。
    feishu_agent_cli_executable: str | None = None
    readonly_repository_path: str | None = None
    readonly_repository_workspace_id: UUID | None = None
    # 只读缓存是后台私有目录；私网和私有 CA 只能由部署管理员显式登记。
    repository_cache_path: str | None = None
    repository_private_origins: tuple[str, ...] = ()
    repository_tls_ca_files: dict[str, str] = Field(default_factory=dict)
    repository_snapshot_max_bytes: int = Field(default=134217728, ge=1048576, le=1073741824)
    # 大仓同步独立于 Run 超时；所有进程需共同访问受控持久卷。
    repository_sync_enabled: bool = False
    repository_sync_timeout_seconds: int = Field(default=900, ge=60, le=3600)
    repository_sync_interval_seconds: int = Field(default=300, ge=30, le=86400)
    repository_sync_max_age_seconds: int = Field(default=900, ge=30, le=172800)
    repository_sync_max_bytes: int = Field(default=2 * 1024**3, ge=1048576, le=100 * 1024**3)
    sandbox_image: str | None = None
    # 仅部署管理员启用；不因保存可写 Git 凭据而隐式获得项目执行权。
    project_sandbox_enabled: bool = False
    # 项目执行可选独立固定镜像（例如 Java）；不替换旧 W09 的 Node/浏览器环境。
    project_sandbox_image: str | None = None
    # 对应的沙箱镜像必须预装固定版本的 Pi/OMP 工具执行器。
    sandbox_native_tools_enabled: bool = False
    project_sandbox_resources: ProjectSandboxResources = Field(default_factory=ProjectSandboxResources)
    # 受信任的部署级 argv，不能由任务或模型提供；Windows 可桥接本地 WSL Docker。
    sandbox_cli_executable: str = "docker"
    sandbox_cli_args: tuple[str, ...] = ()
    sandbox_workspace_id: UUID | None = None
    sandbox_repository_id: str | None = None
    sandbox_repository_archive: str | None = None
    sandbox_repository_revision: str | None = None
    sandbox_repository_sha256: str | None = None
    sandbox_browser_port: int | None = Field(default=None, ge=1024, le=65535)
    github_app_id: str | None = None
    github_installation_id: int | None = None
    github_private_key: SecretStr | None = None
    github_remote_id: str | None = None
    github_repository: str | None = None
    github_target_branch: str | None = None
    # 多连接可并存，凭据以独立引用注入；不要求整个部署只选一个代码托管平台。
    hosting_connections: tuple[HostingConnectionSettings, ...] = ()
    hosting_credentials: dict[str, SecretStr] = Field(default_factory=dict, repr=False, exclude=True)
    # 由管理员明确登记的 API 令牌文件，不复制进 Git 凭据、模型或任务记录。
    hosting_api_token_files: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)
    console_public_url: str | None = None

    @field_validator(
        "object_storage_access_key_id",
        "object_storage_access_key_secret",
        "object_storage_security_token",
        "deepseek_api_key",
        "context_encryption_key",
        "context_cursor_key",
        "workspace_invite_key",
        "github_private_key",
        mode="before",
    )
    @classmethod
    def normalize_blank_secrets(cls, value: object) -> object:
        """把示例配置中的空值视为未配置，避免创建空静态凭据。"""
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        """显式启用时失败即停止，不回退到宿主机其他账号或模型。"""
        if self.run_workspace_model_keys_only and self.deepseek_api_key is not None:
            raise ValueError("仅使用工作空间模型密钥时不能配置全局 DeepSeek 密钥")
        if self.run_runtime == "pi":
            if not self.pi_runtime_entry or not self.pi_model or not self.pi_model.strip():
                raise ValueError("启用 Pi 必须配置入口和明确的模型名")
            if not self.run_workspace_model_keys_only and self.deepseek_api_key is None:
                raise ValueError("启用 Pi 必须配置后端 DeepSeek 密钥，或显式使用工作空间模型密钥")
        return self

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        """部分配置拒绝启动，不能悄悄降级到机器上已有的登录凭据。"""
        if (self.context_encryption_key is None) != (self.context_cursor_key is None):
            raise ValueError("上下文加密与签名密钥必须同时配置")
        if self.feishu_reader_backend == "cli" and not self.feishu_cli_executable:
            raise ValueError("启用飞书 CLI 读取必须配置原生二进制的绝对路径")
        if self.context_cursor_key and len(self.context_cursor_key.get_secret_value().encode()) < 32:
            raise ValueError("上下文签名密钥至少 32 字节")
        if self.workspace_invite_key and len(self.workspace_invite_key.get_secret_value().encode()) < 32:
            raise ValueError("工作空间邀请签名密钥至少 32 字节")
        if self.console_public_url:
            url = urlsplit(self.console_public_url)
            if (
                url.scheme not in {"https", "http"}
                or not url.netloc
                or url.query
                or url.fragment
                or url.username
                or url.password
            ):
                raise ValueError("Console 公网地址必须是无凭据与查询参数的 HTTP(S) 地址")
            if self.environment == "production" and url.scheme != "https":
                raise ValueError("生产审批链接必须使用 HTTPS")
        if self.readonly_repository_path and not self.readonly_repository_workspace_id:
            raise ValueError("只读仓库必须指定获授权工作空间")
        if self.repository_sync_enabled and (
            not self.repository_cache_path
            or self.repository_sync_max_age_seconds <= self.repository_sync_interval_seconds
        ):
            raise ValueError("服务端仓库同步需要持久缓存目录，提交有效期必须长于同步间隔")
        if self.project_sandbox_enabled and (not self.sandbox_image or not self.repository_cache_path):
            raise ValueError("项目隔离执行必须配置固定镜像和只读仓库缓存")
        if self.sandbox_native_tools_enabled and not self.sandbox_image:
            raise ValueError("原生编码工具必须配置受管沙箱镜像")
        if self.project_sandbox_resources != ProjectSandboxResources() and not self.project_sandbox_image:
            raise ValueError("项目资源预算必须绑定独立固定镜像，不能覆盖默认沙箱")
        if self.sandbox_image and (
            not self.sandbox_workspace_id
            or not self.sandbox_repository_id
            or not self.sandbox_repository_archive
            or not self.sandbox_repository_revision
            or not self.sandbox_repository_sha256
        ):
            raise ValueError("沙箱必须配置工作空间、固定镜像与仓库快照")
        identities = [(item.workspace_id, item.remote_id) for item in self.hosting_connections]
        if len(set(identities)) != len(identities):
            raise ValueError("同一工作空间的代码托管远端 ID 不能重复")
        return self

    @model_validator(mode="after")
    def validate_computer(self) -> Self:
        if self.computer_supervisor_url is None:
            return self
        endpoint = urlsplit(self.computer_supervisor_url)
        tokens = (
            self.computer_supervisor_token.get_secret_value() if self.computer_supervisor_token else "",
            self.computer_master_token.get_secret_value() if self.computer_master_token else "",
        )
        if (
            endpoint.scheme != "http"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
            or any(token != token.strip() for token in tokens)
            or min(map(len, tokens)) < 32
            or tokens[0] == tokens[1]
        ):
            raise ValueError("电脑服务需要内网 HTTP 地址和两个不同的至少 32 字符的凭据")
        if any(
            len(scope) != 64 or any(c not in "0123456789abcdef" for c in scope) for scope in self.computer_feishu_scopes
        ):
            raise ValueError("飞书电脑权限必须绑定完整协调话题 ID")
        for origin in self.computer_browser_origins:
            target = urlsplit(origin)
            if (
                target.scheme not in {"http", "https"}
                or not target.hostname
                or target.username
                or target.password
                or origin != f"{target.scheme}://{target.netloc}"
            ):
                raise ValueError("浏览器测试站点必须是 HTTP(S) 来源，不能包含路径或凭据")
        return self

    @model_validator(mode="after")
    def validate_object_storage(self) -> Self:
        """启用对象存储时一次性报告缺失配置，避免请求期才失败。"""
        if self.object_storage_provider == "disabled":
            return self

        required_values = {
            "bucket": self.object_storage_bucket,
            "region": self.object_storage_region,
            "endpoint": self.object_storage_endpoint,
        }
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            raise ValueError(f"aliyun_oss 缺少对象存储配置：{', '.join(missing)}")

        endpoint = urlsplit(self.object_storage_endpoint or "")
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.netloc
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("对象存储 Endpoint 必须是无路径、查询参数和片段的 HTTP(S) 地址")
        if self.environment == "production" and endpoint.scheme != "https":
            raise ValueError("生产对象存储 Endpoint 必须使用 HTTPS")

        has_access_key_id = self.object_storage_access_key_id is not None
        has_access_key_secret = self.object_storage_access_key_secret is not None
        if has_access_key_id != has_access_key_secret:
            raise ValueError("对象存储 AccessKey ID 与 Secret 必须同时配置")
        if self.object_storage_security_token is not None and not has_access_key_id:
            raise ValueError("对象存储 Security Token 必须与 AccessKey 同时配置")
        return self

    @property
    def resolved_object_storage_prefix(self) -> str:
        """生成稳定部署前缀，避免不同环境写入同一逻辑路径。"""
        return self.object_storage_prefix or f"weora/{self.environment}"


@lru_cache
def get_settings() -> AppSettings:
    """每个进程只解析一次环境变量，测试仍可显式注入独立配置。"""
    return AppSettings()
