"""TeamAI 1104f305 的 Rule 投递路径和文件格式。"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from weora_cli.collab_client import CollabError
from weora_cli.resource_delivery import END, START, Object, values
from weora_cli.skill_adapters import TOOLS, _openclaw_workspace, tool_home


@dataclass(frozen=True)
class RuleTool:
    # 路径以工具用户根或 Git 检出根为基准。
    user: str | None
    project: str | None
    format: str = "verbatim"
    inline: str | None = None
    hook: str | None = None


CODEX_TOOLS = {"codex", "codex-internal", "tcodex"}
RULE_TOOLS = {
    "claude": RuleTool("rules", ".claude/rules"),
    "claude-internal": RuleTool("rules", ".claude-internal/rules"),
    "tclaude": RuleTool("rules", ".tclaude/rules"),
    **{tool: RuleTool(None, None, inline="AGENTS.md", hook="codex") for tool in sorted(CODEX_TOOLS)},
    "cursor": RuleTool("rules", ".cursor/rules", "cursor"),
    "copilot": RuleTool("instructions", ".github/instructions", "copilot"),
    "joycode": RuleTool(None, ".joycode/rules", "joycode", inline="rules.txt"),
    "qoder": RuleTool("rules", ".qoder/rules", "qoder"),
    "qoder-cn": RuleTool("rules", ".qoder/rules", "qoder"),
    "kiro": RuleTool("steering", ".kiro/steering", "kiro"),
    "trae": RuleTool("user_rules", ".trae/rules", "trae"),
    "trae-cn": RuleTool("user_rules", ".trae/rules", "trae"),
    "zcode": RuleTool(None, None, inline="AGENTS.md", hook="process"),
    "omp": RuleTool("rules", ".omp/rules", "omp"),
    "pi": RuleTool(None, None, inline="AGENTS.md", hook="extension"),
    "codebuddy": RuleTool("rules", ".codebuddy/rules", "codebuddy"),
    "workbuddy": RuleTool("rules", ".codebuddy/rules", "codebuddy"),
    "openclaw": RuleTool(None, None, inline="AGENTS.md"),
    "hermes": RuleTool(None, None, inline="SOUL.md"),
    "dsh": RuleTool(None, None, inline="AGENTS.md", hook="cordis"),
    "opencode": RuleTool("rules", ".opencode/rules"),
    "muse": RuleTool(None, None, inline="AGENTS.md", hook="muse"),
    "agy": RuleTool("rules", ".agents/rules", "agy"),
}


def home(runtime: str) -> Path:
    if runtime in TOOLS:
        return tool_home(runtime)
    env = {
        "codex-internal": "CODEX_INTERNAL_HOME",
        "tcodex": "TCODEX_HOME",
        "claude-internal": "CLAUDE_INTERNAL_CONFIG_DIR",
        "tclaude": "TCLAUDE_CONFIG_DIR",
    }[runtime]
    return Path(os.environ.get(env, str(Path.home() / f".{runtime}"))).expanduser().resolve()


def rule_directory(runtime: str, root: Path | None) -> Path | None:
    tool = RULE_TOOLS[runtime]
    directory = tool.user if root is None else tool.project
    return (home(runtime) if root is None else root) / directory if directory else None


def inline_file(runtime: str) -> Path | None:
    name = RULE_TOOLS[runtime].inline
    if name is None:
        return None
    origin = home(runtime)
    if runtime == "openclaw":
        origin = _openclaw_workspace(origin)
    if runtime == "pi" and (origin / "AGENTS.override.md").is_file():
        return origin / "AGENTS.override.md"
    return origin / name


def stem(filename: str) -> str | None:
    for suffix in (".instructions.md", ".mdc", ".md"):
        if filename.endswith(suffix):
            return filename.removesuffix(suffix)
    return None


def native_filename(format_id: str, name: str) -> str:
    if format_id in {"kiro", "omp"}:
        name = name.replace("/", ".")
    extension = {"cursor": ".mdc", "joycode": ".mdc", "copilot": ".instructions.md"}.get(format_id, ".md")
    return name + extension


def split_markdown(text: str) -> tuple[str, str]:
    text = text.removeprefix("\ufeff")
    boundary = re.match(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|$)", text, re.MULTILINE | re.DOTALL)
    if boundary is None:
        return "", text.strip()
    return boundary[0], text[boundary.end() :].strip()


def expand_globs(paths: list[str]) -> list[str]:
    # 这些宿主按逗号分割字段，需要先展开花括号备选项。
    expanded: list[str] = []
    for pattern in paths:
        variants = [pattern]
        for _ in range(16):
            pieces: list[str] = []
            for variant in variants:
                matched = re.search(r"\{([^{}]*,[^{}]*)\}", variant)
                if matched:
                    pieces.extend(
                        variant[: matched.start()] + part + variant[matched.end() :] for part in matched[1].split(",")
                    )
                else:
                    pieces.append(variant)
                if len(pieces) > 256:
                    raise CollabError("RESOURCE_GLOB_LIMIT", "规则通配符包含过多备选项。")
            if pieces == variants:
                break
            variants = pieces
        expanded.extend(variants)
    return list(dict.fromkeys(expanded))


def omp_description(body: str, paths: list[str]) -> str:
    fenced = False
    for line in body.splitlines():
        if re.match(r"\s*(```|~~~)", line):
            fenced = not fenced
        elif not fenced and (heading := re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)):
            return heading[1]
    return "Team rule for files matching " + ", ".join(paths)


def render_native(format_id: str, rule: Object, raw: str, directory: Path) -> str:
    location = (directory / str(rule["name"])).as_posix()
    body = str(rule["body"]).replace(START, "").replace(END, "").replace("${TEAMAI_RULE_DIR}", location)
    cursor_paths = rule.get("cursor_paths") if format_id == "cursor" else None
    paths = values(rule["paths"] if cursor_paths is None else cursor_paths)
    if format_id == "verbatim":
        header, old_body = split_markdown(raw)
        if old_body == body and "${TEAMAI_RULE_DIR}" not in raw:
            return raw
        return (header.rstrip() + "\n\n" if header else "") + body + "\n"
    quoted = ", ".join(json.dumps(path, ensure_ascii=False) for path in paths)
    if format_id == "copilot":
        header_lines = ["applyTo: " + json.dumps(", ".join(paths) or "**", ensure_ascii=False)]
    elif format_id == "kiro":
        header_lines = ["inclusion: fileMatch", f"fileMatchPattern: [{quoted}]"] if paths else ["inclusion: always"]
    elif format_id == "codebuddy":
        header_lines = (
            ["alwaysApply: false", "paths:", *("  - " + json.dumps(p, ensure_ascii=False) for p in paths)]
            if paths
            else ["alwaysApply: true"]
        )
    elif format_id == "omp":
        header_lines = (
            [
                "description: " + json.dumps(omp_description(body, paths), ensure_ascii=False),
                f"globs: [{quoted}]",
            ]
            if paths
            else ["alwaysApply: true"]
        )
    elif format_id in {"qoder", "agy"}:
        globs = ", ".join(expand_globs(paths))
        if not globs:
            header_lines = ["trigger: always_on"]
        elif format_id == "qoder":
            header_lines = ["trigger: glob", f"glob: {globs}"]
        else:
            description = "Applies when reading or editing files matching " + globs
            header_lines = ["trigger: model_decision", "description: " + json.dumps(description, ensure_ascii=False)]
    else:
        header_lines = ["alwaysApply: true"]
        if paths:
            globs = (
                json.dumps(", ".join(paths), ensure_ascii=False)
                if format_id == "cursor"
                else ", ".join(expand_globs(paths))
            )
            header_lines = [f"globs: {globs}", "alwaysApply: false"]
    return "---\n" + "\n".join(header_lines) + "\n---\n\n" + body + "\n"
