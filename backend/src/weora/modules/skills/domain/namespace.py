"""校验团队 Skill 路径中的命名空间，不变更 Skill 的安装名。"""

from weora.modules.skills.domain.errors import SkillNamespaceInvalidError


def normalize_skill_namespace(value: str) -> str:
    """接受团队树下的多级相对目录，统一 Windows 分隔符。"""
    if not value:
        return ""
    path = value.replace("\\", "/")
    if len(path) > 512 or not path.isprintable() or any(char in path for char in '<>:"|?*#'):
        raise SkillNamespaceInvalidError
    if any(not part or part.startswith(".") or part.endswith(".") or part != part.strip() for part in path.split("/")):
        raise SkillNamespaceInvalidError
    return path
