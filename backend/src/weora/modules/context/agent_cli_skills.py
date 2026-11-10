"""按需向 Agent 提供与飞书 CLI 同版本的官方说明。"""

from pathlib import Path
from typing import cast

import yaml

from weora.modules.context.domain import Json
from weora.modules.context.feishu_cli import CLI_VERSION
from weora.modules.tools.application.gateway import ToolScope, ToolSpec, failure

_ROOT = Path(__file__).with_name("lark_cli_skills") / "skills"
_PAGE_SIZE = 6_000

FEISHU_CLI_SKILL = ToolSpec(
    "lark.cli.skill",
    "查看官方飞书 CLI Skill。传 {} 列出 Skill；传 path 读取 Markdown，"
    "例如 lark-sheets/SKILL.md；有 next_offset 时传 offset 继续。"
    "引用的相对路径请以 Skill 目录为起点转换为目录内路径。"
    "Weora 中通过 lark.cli.exec 执行命令，身份使用当前 Run 已绑定的 Bot。",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 512},
            "offset": {"type": "integer", "minimum": 0},
        },
        "dependentRequired": {"offset": ["path"]},
    },
)


async def read_feishu_cli_skill(_scope: ToolScope, arguments: Json) -> Json:
    if "path" not in arguments:
        items = []
        for entry in sorted(_ROOT.glob("lark-*/SKILL.md")):
            # 索引只返回简介，正文和参考文件由 Agent 按任务取用。
            frontmatter = yaml.safe_load(entry.read_text(encoding="utf-8").split("---", 2)[1])
            items.append({
                "name": entry.parent.name,
                "path": f"{entry.parent.name}/SKILL.md",
                "description": str(frontmatter["description"]),
            })
        return {"ok": True, "version": CLI_VERSION, "skills": items}

    path = cast(str, arguments["path"])
    segments = path.split("/")
    if (
        len(segments) < 2
        or not segments[0].startswith("lark-")
        or not path.endswith(".md")
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(character in path for character in ("\\", ":", "\0"))
    ):
        return failure("FEISHU_SKILL_PATH_INVALID")
    file = _ROOT.joinpath(*segments).resolve()
    if not file.is_relative_to(_ROOT.resolve()) or not file.is_file():
        return failure("FEISHU_SKILL_NOT_FOUND")
    content = file.read_text(encoding="utf-8")
    offset = cast(int, arguments.get("offset", 0))
    end = offset + _PAGE_SIZE
    return {
        "ok": True,
        "path": path,
        "version": CLI_VERSION,
        "content": content[offset:end],
        "next_offset": end if end < len(content) else None,
    }
