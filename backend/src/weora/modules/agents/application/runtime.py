"""从不可变 AgentVersion 和 Skill 包装配运行时输入。"""

import hashlib
from collections.abc import Awaitable, Callable
from io import BytesIO
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from weora.modules.agents.application.ports import (
    AgentRuntimeContext,
    AgentSkillBindingRecord,
    AgentVersionRecord,
    RuntimeSkillDefinition,
    RuntimeSkillReference,
)
from weora.modules.agents.domain.errors import (
    AgentRuntimeMaterialInvalidError,
    AgentRuntimeStorageUnavailableError,
)
from weora.modules.skills.domain.errors import SkillRegistryError, SkillStorageUnavailableError

_TEXT_REFERENCE_SUFFIXES = frozenset({".md", ".txt", ".json", ".yaml", ".yml"})


class AgentRuntimeContextAssembler:
    """验证每个 Skill 发布包后，生成与具体 Agent Runtime 解耦的输入。"""

    def __init__(self, read_archive: Callable[[UUID, UUID], Awaitable[bytes]]) -> None:
        self._read_archive = read_archive

    async def assemble(self, version: AgentVersionRecord) -> AgentRuntimeContext:
        """按绑定顺序装配 Skill；任何一个包不可信时整次装配失败。"""
        skills = tuple([await self._load_skill(version.workspace_id, binding) for binding in version.skills])
        return AgentRuntimeContext(
            agent_version_id=version.id,
            agent_id=version.agent_id,
            workspace_id=version.workspace_id,
            display_name=version.display_name,
            system_prompt=version.system_prompt,
            skills=skills,
        )

    async def _load_skill(
        self,
        workspace_id: UUID,
        binding: AgentSkillBindingRecord,
    ) -> RuntimeSkillDefinition:
        """下载并校验固定包，只暴露 SKILL.md 与文本 references。"""
        try:
            archive_bytes = await self._read_archive(workspace_id, binding.skill_version_id)
        except SkillStorageUnavailableError as exc:
            raise AgentRuntimeStorageUnavailableError from exc
        except SkillRegistryError as exc:
            raise AgentRuntimeMaterialInvalidError from exc

        if (
            len(archive_bytes) != binding.package_size
            or hashlib.sha256(archive_bytes).hexdigest() != binding.package_sha256
        ):
            raise AgentRuntimeMaterialInvalidError

        try:
            with ZipFile(BytesIO(archive_bytes)) as archive:
                root = f"{binding.skill_slug}/"
                instructions = archive.read(f"{root}SKILL.md").decode("utf-8")
                references = tuple(
                    RuntimeSkillReference(
                        path=name.removeprefix(root),
                        content=archive.read(name).decode("utf-8"),
                    )
                    for name in sorted(archive.namelist())
                    if self._is_text_reference(name, root)
                )
        except (BadZipFile, KeyError, UnicodeDecodeError, ValueError) as exc:
            raise AgentRuntimeMaterialInvalidError from exc

        return RuntimeSkillDefinition(
            skill_version_id=binding.skill_version_id,
            name=binding.skill_slug,
            version=binding.version,
            content_sha256=binding.content_sha256,
            instructions=instructions,
            references=references,
        )

    @staticmethod
    def _is_text_reference(path: str, root: str) -> bool:
        """脚本与资产不注入提示词，只开放低风险文本参考文件。"""
        if not path.startswith(f"{root}references/") or path.endswith("/"):
            return False
        suffix = "." + path.rsplit(".", maxsplit=1)[-1].lower() if "." in path else ""
        return suffix in _TEXT_REFERENCE_SUFFIXES
