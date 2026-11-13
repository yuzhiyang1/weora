"""Skill Registry 对外保持稳定的领域错误。"""


class SkillRegistryError(Exception):
    """所有 Skill Registry 可预期错误的基类。"""

    code = "SKILL_REGISTRY_ERROR"
    message = "Skill 操作未完成"


class SkillPackageTooLargeError(SkillRegistryError):
    """上传包或解压内容超过安全限制。"""

    code = "SKILL_PACKAGE_TOO_LARGE"
    message = "Skill 包超过允许的大小或文件数量限制"


class SkillArchiveInvalidError(SkillRegistryError):
    """上传内容不是可完整读取的 ZIP 归档。"""

    code = "SKILL_ARCHIVE_INVALID"
    message = "Skill 包不是有效的 ZIP 文件"


class SkillArchiveUnsafePathError(SkillRegistryError):
    """归档包含可能逃逸 Skill 根目录的路径。"""

    code = "SKILL_ARCHIVE_UNSAFE_PATH"
    message = "Skill 包包含不安全的文件路径或文件类型"


class SkillRootAmbiguousError(SkillRegistryError):
    """归档没有唯一的顶层 Skill 目录。"""

    code = "SKILL_ROOT_AMBIGUOUS"
    message = "Skill 包必须且只能包含一个顶层目录"


class SkillManifestMissingError(SkillRegistryError):
    """Skill 根目录缺少 SKILL.md。"""

    code = "SKILL_MANIFEST_MISSING"
    message = "Skill 根目录中缺少 SKILL.md"


class SkillManifestInvalidError(SkillRegistryError):
    """SKILL.md Frontmatter 缺失或字段不合法。"""

    code = "SKILL_MANIFEST_INVALID"
    message = "SKILL.md 中的 name 或 description 不合法"


class SkillSlugConflictError(SkillRegistryError):
    """同一工作空间中已存在相同 Skill 标识。"""

    code = "SKILL_SLUG_CONFLICT"
    message = "当前工作空间已存在同名 Skill"


class SkillDraftNotReadyError(SkillRegistryError):
    """发布前不存在可用草稿。"""

    code = "SKILL_DRAFT_NOT_READY"
    message = "当前 Skill 没有可发布的草稿"


class SkillVersionInvalidError(SkillRegistryError):
    """发布版本不符合 SemVer 约束。"""

    code = "SKILL_VERSION_INVALID"
    message = "版本号必须使用 SemVer，例如 0.1.0"


class SkillCursorInvalidError(SkillRegistryError):
    """列表游标不是平台签发的合法格式。"""

    code = "SKILL_CURSOR_INVALID"
    message = "分页游标不合法或已经损坏"


class SkillVersionConflictError(SkillRegistryError):
    """同一 Skill 中已存在该版本号。"""

    code = "SKILL_VERSION_CONFLICT"
    message = "该 Skill 版本已经发布"


class SkillArchivedError(SkillRegistryError):
    """已归档 Skill 不允许继续写入或发布。"""

    code = "SKILL_ARCHIVED"
    message = "已归档 Skill 不能执行该操作"


class SkillContributionStaleError(SkillRegistryError):
    """候选包提交后，原草稿或发布版本已经变化。"""

    code = "SKILL_CONTRIBUTION_STALE"
    message = "当前草稿或发布版本已变更，请让上传人重新提交候选包。"


class SkillDraftChangedError(SkillRegistryError):
    """预览或确认发布后，当前草稿已被其他操作替换。"""

    code = "SKILL_DRAFT_CHANGED"
    message = "当前草稿已变更，请刷新后重新查看和确认。"


class SkillNotFoundError(SkillRegistryError):
    """当前工作空间中不存在目标 Skill 或版本。"""

    code = "SKILL_NOT_FOUND"
    message = "Skill 不存在"


class SkillObjectMissingError(SkillRegistryError):
    """元数据存在但对象存储中的发布包缺失。"""

    code = "SKILL_OBJECT_MISSING"
    message = "Skill 包文件缺失，请联系管理员"


class SkillStorageUnavailableError(SkillRegistryError):
    """当前部署没有启用对象存储能力。"""

    code = "SKILL_STORAGE_UNAVAILABLE"
    message = "对象存储尚未配置，暂时不能上传或下载 Skill"


class WorkspaceAccessDeniedError(SkillRegistryError):
    """当前成员不能访问路径指定的工作空间。"""

    code = "WORKSPACE_ACCESS_DENIED"
    message = "无权访问该工作空间"


class SkillPermissionDeniedError(SkillRegistryError):
    """成员角色不允许执行当前写操作。"""

    code = "SKILL_PERMISSION_DENIED"
    message = "当前成员没有管理 Skill 的权限"
