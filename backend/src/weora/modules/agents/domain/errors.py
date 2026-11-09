"""Agent 模块对外保持稳定的领域错误。"""


class AgentRegistryError(Exception):
    """所有 Agent 资产可预期错误的基类。"""

    code = "AGENT_REGISTRY_ERROR"
    message = "Agent 操作未完成"


class AgentNotFoundError(AgentRegistryError):
    """当前工作空间中不存在目标 Agent 或版本。"""

    code = "AGENT_NOT_FOUND"
    message = "Agent 不存在"


class AgentPermissionDeniedError(AgentRegistryError):
    """成员角色不允许管理 Agent。"""

    code = "AGENT_PERMISSION_DENIED"
    message = "当前成员没有管理 Agent 的权限"


class AgentWorkspaceAccessDeniedError(AgentRegistryError):
    """当前成员不能访问路径指定的工作空间。"""

    code = "WORKSPACE_ACCESS_DENIED"
    message = "无权访问该工作空间"


class AgentSkillVersionInvalidError(AgentRegistryError):
    """绑定包含未发布、重复或其他工作空间的 SkillVersion。"""

    code = "AGENT_SKILL_VERSION_INVALID"
    message = "只能绑定当前工作空间中已发布且不重复的 Skill 版本"


class AgentVersionInvalidError(AgentRegistryError):
    """Agent 发布版本不符合 SemVer 约束。"""

    code = "AGENT_VERSION_INVALID"
    message = "版本号必须使用 SemVer，例如 0.1.0"


class AgentVersionConflictError(AgentRegistryError):
    """同一 Agent 已存在相同版本号。"""

    code = "AGENT_VERSION_CONFLICT"
    message = "该 Agent 版本已经发布"


class AgentRuntimeMaterialInvalidError(AgentRegistryError):
    """绑定的 Skill 包缺失、损坏或与发布快照不一致。"""

    code = "AGENT_RUNTIME_MATERIAL_INVALID"
    message = "Agent 运行材料缺失或完整性校验失败"


class AgentRuntimeStorageUnavailableError(AgentRegistryError):
    """当前部署未配置运行时读取所需的对象存储。"""

    code = "AGENT_RUNTIME_STORAGE_UNAVAILABLE"
    message = "对象存储尚未配置，暂时不能装配 Agent 运行上下文"
