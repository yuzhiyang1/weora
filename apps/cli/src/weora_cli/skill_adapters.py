"""TeamAI CLI 1104f305 的工具路径与 Hook 适配。

来源：src/types.ts DEFAULT_TOOL_PATHS、src/tools.ts TOOLS、src/hooks.ts、
src/pi-hooks.ts、src/omp-hooks.ts、src/opencode-hooks.ts、src/agy-hooks.ts、src/kiro-hooks.ts、
src/openclaw-hooks.ts、src/hermes-hooks.ts、src/hermes-config.ts 和 src/dsh-hooks.ts。
版权与 MIT 许可见 TEAMAI-LICENSE.txt。
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from uuid import uuid4

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from weora_cli.collab_client import CollabError, as_object, write_json
from weora_cli.commands import JsonValue


@dataclass(frozen=True)
class Tool:
    project: str
    home: str
    user: str = "skills"
    variables: tuple[str, ...] = ()
    hook: str = "settings.json"
    binary: str = ""


# 从 TeamAI 的默认写入表保留项目与用户两套路径。
TOOLS = {
    "claude": Tool(".claude/skills", ".claude", variables=("CLAUDE_CONFIG_DIR",), binary="claude"),
    "codex": Tool(".agents/skills", ".codex", variables=("CODEX_HOME",), hook="hooks.json"),
    "cursor": Tool(".cursor/skills", ".cursor", hook="cursor", binary="cursor"),
    "codebuddy": Tool(".codebuddy/skills", ".codebuddy", variables=("CODEBUDDY_CONFIG_DIR",)),
    "workbuddy": Tool(".workbuddy/skills", ".workbuddy", variables=("WORKBUDDY_CONFIG_DIR",)),
    "copilot": Tool(".github/skills", ".copilot", variables=("COPILOT_HOME",), hook="copilot"),
    "opencode": Tool(".opencode/skills", ".config/opencode", variables=("OPENCODE_CONFIG_DIR",), hook="opencode"),
    "pi": Tool(".pi/skills", ".pi/agent", variables=("PI_CODING_AGENT_DIR",), hook="pi"),
    "omp": Tool(".omp/skills", ".omp/agent", hook="pi"),
    "openclaw": Tool("skills", ".openclaw", variables=("OPENCLAW_STATE_DIR",), hook="openclaw"),
    "hermes": Tool(".hermes/skills", ".hermes", variables=("HERMES_HOME",), hook="hermes"),
    "dsh": Tool(".dsh/skills", ".dsh", variables=("DSH_HOME",), hook="dsh"),
    "qoder": Tool(".qoder/skills", ".qoder", variables=("QODER_CONFIG_DIR",)),
    "qoder-cn": Tool(".qoder/skills", ".qoder-cn", variables=("QODERCN_CONFIG_DIR",)),
    "kiro": Tool(".kiro/skills", ".kiro", hook="kiro", binary="kiro-cli"),
    "trae": Tool(".trae/skills", ".trae", hook="trae"),
    "trae-cn": Tool(".trae/skills", ".trae-cn", hook="trae"),
    "zcode": Tool(".zcode/skills", ".zcode", hook="cli/config.json"),
    "joycode": Tool(".joycode/skills", ".joycode", variables=("JOYCODE_HOME",), hook="hooks.json"),
    "muse": Tool(".agents/skills", ".config/muse"),
    "agy": Tool(".agents/skills", ".gemini/config", hook="agy"),
}

EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "PostToolUse": "tool", "Stop": "stop"}
USER_ONLY = {"hermes", "dsh"}
# TeamAI src/hooks.ts CLAUDE_TO_CURSOR_EVENTS。
CURSOR_EVENTS = {
    "SessionStart": "sessionStart",
    "Stop": "stop",
    "PostToolUse": "postToolUse",
    "UserPromptSubmit": "beforeSubmitPrompt",
}


def tool_home(runtime: str) -> Path:
    tool = TOOLS[runtime]
    override = next((os.environ[key] for key in tool.variables if os.environ.get(key)), None)
    if runtime == "hermes":
        suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
        native = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / f"hermes{suffix}"
            if os.name == "nt"
            else Path.home() / f".hermes{suffix}"
        ).resolve()
        home = Path(override).expanduser().resolve() if override else native
        marker = (native if home.is_relative_to(native) else home) / "active_profile"
        if home.parent.name != "profiles" and marker.is_file():
            profile = marker.read_text(encoding="utf-8").strip()
            if profile not in {"", "default", ".", ".."} and Path(profile).name == profile:
                target = home / "profiles" / profile
                if target.is_dir():
                    return target
        return home
    if override:
        return Path(override).expanduser().resolve()
    if runtime == "omp":
        base = Path(os.environ.get("PI_CONFIG_DIR", str(Path.home() / ".omp")))
        omp_profile = os.environ.get("OMP_PROFILE") or os.environ.get("PI_PROFILE")
        return (base / "profiles" / omp_profile / "agent" if omp_profile else base / "agent").resolve()
    if runtime in {"opencode", "muse"}:
        return (Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / runtime).resolve()
    if runtime == "openclaw":
        profile = os.environ.get("OPENCLAW_PROFILE", "default")
        name = ".openclaw" if profile == "default" else f".openclaw-{profile}"
        return (Path(os.environ.get("OPENCLAW_HOME", str(Path.home()))) / name).resolve()
    if runtime == "workbuddy":
        homes = [Path.home() / ".workbuddy", Path.home() / ".workbuddy-ai"]
        active = [
            ((directory / "last-launch.json").stat().st_mtime_ns, directory)
            for directory in homes
            if (directory / "last-launch.json").is_file()
        ]
        if active:
            return max(active)[1].resolve()
        return next((directory for directory in homes if directory.is_dir()), homes[0]).resolve()
    for key in {"qoder": ("QODER_CLI_HOME", ".qoder"), "qoder-cn": ("QODERCN_CLI_HOME", ".qoder-cn")}.get(runtime, ())[
        :1
    ]:
        if value := os.environ.get(key):
            return (Path(value) / tool.home).resolve()
    return (Path.home() / tool.home).resolve()


def destination(runtime: str, root: Path | None) -> Path:
    if runtime == "openclaw" and root is None:
        return _openclaw_workspace(tool_home(runtime)) / "skills"
    if runtime in USER_ONLY:
        # 只使用上游确认会被原生客户端扫描的目录。
        return tool_home(runtime) / "skills"
    return root / TOOLS[runtime].project if root is not None else tool_home(runtime) / TOOLS[runtime].user


def skill_roots(runtime: str, cwd: Path, project: Path | None) -> set[Path]:
    roots = {destination(runtime, None)}
    if project and cwd.is_relative_to(project):
        for level in (cwd, *cwd.parents):
            if not level.is_relative_to(project):
                break
            roots.add(destination(runtime, level))
            if runtime == "codex":
                roots.add(level / ".codex/skills")
    if runtime in {"codex", "muse", "agy"}:
        roots.add(Path.home() / ".agents/skills")
    if runtime in {"opencode", "muse"}:
        roots.add(Path.home() / ".claude/skills")
    return roots


def _command(runtime: str, event: str) -> list[str]:
    return [sys.executable, "-m", "weora_cli", "skill", "hook", "--runtime", runtime, "--event", event]


def _shell_command(runtime: str, event: str) -> str:
    command = _command(runtime, event)
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _settings(path: Path) -> dict[str, JsonValue]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else {}
    except (OSError, ValueError, CollabError) as exc:
        raise CollabError("SKILL_HOOK_CONFIG_INVALID", f"无法读取 Hook 配置 {path}") from exc


def _openclaw_config(home: Path) -> Path:
    return Path(os.environ.get("OPENCLAW_CONFIG_PATH", str(home / "openclaw.json"))).expanduser().resolve()


def _openclaw_workspace(home: Path, config: dict[str, JsonValue] | None = None) -> Path:
    config = _settings(_openclaw_config(home)) if config is None else config
    agents = as_object(config.get("agents", {}))
    defaults = as_object(agents.get("defaults", {}))
    directory = defaults.get("workspace")
    return (
        Path(
            directory
            if isinstance(directory, str) and directory.strip()
            else os.environ.get("OPENCLAW_WORKSPACE_DIR", str(home / "workspace"))
        )
        .expanduser()
        .resolve()
    )


def _yaml_document(path: Path, default: CommentedMap | CommentedSeq) -> tuple[YAML, CommentedMap | CommentedSeq]:
    yaml = YAML()
    try:
        data = yaml.load(path.read_text(encoding="utf-8-sig")) if path.exists() else None
    except (OSError, YAMLError) as exc:
        raise CollabError("SKILL_HOOK_CONFIG_INVALID", f"无法读取 Hook 配置 {path}") from exc
    if data is None:
        return yaml, default
    if not isinstance(data, type(default)):
        raise CollabError("SKILL_HOOK_CONFIG_INVALID", f"Hook 配置结构无效 {path}")
    return yaml, data


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        if path.exists():
            temp.chmod(path.stat().st_mode & 0o777)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _write_yaml(path: Path, yaml: YAML, data: CommentedMap | CommentedSeq) -> None:
    stream = StringIO()
    yaml.dump(data, stream)
    _write_text(path, stream.getvalue())


def _hermes_hook(home: Path) -> Path:
    path = home / "config.yaml"
    yaml, config = _yaml_document(path, CommentedMap())
    assert isinstance(config, CommentedMap)
    allowlist_path = home / "shell-hooks-allowlist.json"
    allowlist = _settings(allowlist_path)
    approvals = allowlist.setdefault("approvals", [])
    hooks = config.setdefault("hooks", CommentedMap())
    if not isinstance(approvals, list) or not isinstance(hooks, dict):
        raise CollabError("SKILL_HOOK_CONFIG_INVALID", "Hermes 的 Hook 或批准列表结构无效。")
    for name, event in {
        "on_session_start": "start",
        "pre_llm_call": "prompt",
        "post_tool_call": "tool",
        "on_session_end": "stop",
    }.items():
        entries = hooks.setdefault(name, CommentedSeq())
        if not isinstance(entries, list):
            raise CollabError("SKILL_HOOK_CONFIG_INVALID", f"Hermes 的 {name} Hook 不是列表。")
        command = _shell_command("hermes", event)
        entries[:] = [entry for entry in entries if not (isinstance(entry, dict) and _owned(entry.get("command")))]
        entries.append({"command": command, "timeout": 10})
        approvals[:] = [
            entry
            for entry in approvals
            if not (isinstance(entry, dict) and entry.get("event") == name and _owned(entry.get("command")))
        ]
        approvals.append({"event": name, "command": command})
    write_json(allowlist_path, allowlist)
    _write_yaml(path, yaml, config)
    return path


def _dsh_hook(home: Path) -> Path:
    path = home / "cordis.patch.yml"
    yaml, rows = _yaml_document(path, CommentedSeq())
    plugin_id = "weora-skills-hooks-claude-code"
    config_path = home / "weora-skills" / "hooks.json"
    # 沿用 TeamAI 的 Cordis 插入行，保留同一行中的其他插件和 YAML 注释。
    assert isinstance(rows, CommentedSeq)
    for row in list(rows):
        inserts = row.get("insert") if isinstance(row, dict) else None
        if isinstance(inserts, list):
            previous = len(inserts)
            inserts[:] = [entry for entry in inserts if not (isinstance(entry, dict) and entry.get("id") == plugin_id)]
            if previous and not inserts:
                rows.remove(row)
    rows.append(
        {
            "insert": [
                {
                    "id": plugin_id,
                    "name": "@deepseek-ai/dsh-hooks-claude-code",
                    "config": {"configPath": str(config_path)},
                }
            ]
        }
    )
    _merge(config_path, "dsh", "settings.json")
    _write_yaml(path, yaml, rows)
    return path


def _openclaw_hook(home: Path) -> Path:
    config_path = _openclaw_config(home)
    config = _settings(config_path)
    plugin_id = "weora-skill-hooks"
    plugins = as_object(config.setdefault("plugins", {}))
    entries = as_object(plugins.setdefault("entries", {}))
    entry = as_object(entries.setdefault(plugin_id, {}))
    allow, deny = plugins.get("allow"), plugins.get("deny")
    if (
        plugins.get("enabled") is False
        or entry.get("enabled") is False
        or (isinstance(allow, list) and allow and plugin_id not in allow)
        or (isinstance(deny, list) and plugin_id in deny)
    ):
        raise CollabError("SKILL_HOOK_CONFIG_INVALID", f"OpenClaw 配置禁止加载 {plugin_id}，请先调整插件权限。")
    load = as_object(plugins.setdefault("load", {}))
    paths = load.setdefault("paths", [])
    if not isinstance(paths, list):
        raise CollabError("SKILL_HOOK_CONFIG_INVALID", "OpenClaw 插件路径不是列表。")
    workspace = _openclaw_workspace(home, config)
    path = home / "weora-skills" / f"{plugin_id}.mjs"
    entry["enabled"] = True
    as_object(entry.setdefault("hooks", {}))["allowConversationAccess"] = True
    if str(path) not in paths:
        paths.append(str(path))
    # TeamAI 的 OpenClaw bridge：会话第一次构建提示词时同步，工具结束时读取结构化事件。
    _write_text(
        path,
        _extension_prelude("openclaw")
        + f"const workspace = {json.dumps(str(workspace))};\n"
        + """
const started = new Map();
function context(ctx) {
  const id = ctx.sessionId || ctx.sessionKey;
  return { session_id: id, cwd: ctx.workspaceDir || started.get(id) || workspace };
}
export default {
  id: "weora-skill-hooks",
  name: "Weora Skill Hooks",
  register(api) {
    api.on("before_prompt_build", async (event, ctx) => {
      const scope = context(ctx);
      if (!started.has(scope.session_id)) await dispatch("start", scope);
      if (started.size >= 1024) started.delete(started.keys().next().value);
      started.set(scope.session_id, scope.cwd);
      await dispatch("prompt", { ...scope, prompt: event.currentUserMessage ?? event.prompt });
    });
    api.on("after_tool_call", async (event, ctx) => {
      await dispatch("tool", { ...context(ctx), tool_name: event.toolName, tool_input: event.params,
        tool_response: event.result, tool_status: event.error ? "failure" : "success" });
    });
    api.on("agent_end", async (_event, ctx) => { await dispatch("stop", context(ctx)); });
  },
};
""",
    )
    write_json(config_path, config)
    return path


def _owned(command: JsonValue) -> bool:
    return isinstance(command, str) and "weora_cli skill hook --runtime " in command


def _owned_handler(handler: dict[str, JsonValue]) -> bool:
    args = handler.get("args")
    return _owned(handler.get("command")) or (
        isinstance(args, list) and args[:5] == ["-m", "weora_cli", "skill", "hook", "--runtime"]
    )


def _merge(path: Path, runtime: str, shape: str) -> None:
    config = _settings(path)
    hooks = as_object(config.setdefault("hooks", {}))
    for name, event in EVENTS.items():
        if shape == "cursor":
            name = CURSOR_EVENTS[name]
        entries = hooks.setdefault(name, [])
        if not isinstance(entries, list):
            raise CollabError("SKILL_HOOK_CONFIG_INVALID", f"Hook 列表格式无效 {path}")
        command = _shell_command(runtime, event)
        if shape in {"cursor", "copilot"}:
            entries[:] = [
                entry
                for entry in entries
                if not _owned(
                    as_object(entry).get("command")
                    or as_object(entry).get("bash")
                    or as_object(entry).get("powershell")
                )
            ]
            if shape == "cursor":
                entries.append({"command": command})
            else:
                args = _command(runtime, event)
                bash = shlex.join([Path(args[0]).as_posix(), *args[1:]])
                powershell = "& '" + args[0].replace("'", "''") + "' " + " ".join(args[1:])
                entries.append({"type": "command", "command": command, "bash": bash, "powershell": powershell})
        else:
            for raw in entries[:]:
                group = as_object(raw)
                handlers = group.get("hooks")
                if isinstance(handlers, list):
                    filtered = [handler for handler in handlers if not _owned_handler(as_object(handler))]
                    if filtered:
                        group["hooks"] = filtered
                    else:
                        entries.remove(raw)
            if shape == "cli/config.json":
                # ZCode 用 process Hook，参数和 stdin 直接传给进程，超时单位为毫秒。
                python, *args = _command(runtime, event)
                entries.append({"hooks": [{"type": "process", "command": python, "args": [*args], "timeoutMs": 10000}]})
            else:
                entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    if shape in {"copilot", "trae"}:
        config.setdefault("version", 1)
    write_json(path, config)


def _extension_prelude(runtime: str) -> str:
    command = json.dumps(_command(runtime, "start")[:-1])
    # TeamAI 的桥接用宿主事件，把同一形状的 JSON 通过 stdin 发给 CLI。
    return (
        'import { spawn } from "node:child_process";\n'
        f"const [python, ...args] = {command};\n"
        "function dispatch(event, data) { return new Promise((done) => {\n"
        '  const child = spawn(python, [...args, event], { stdio: ["pipe", "ignore", "ignore"], windowsHide: true });\n'
        "  const timer = setTimeout(() => { child.kill(); done(); }, 8000);\n"
        "  const finish = () => { clearTimeout(timer); done(); };\n"
        '  child.on("error", finish); child.on("close", finish); child.stdin.on("error", finish);\n'
        "  child.stdin.end(JSON.stringify(data));\n}); }\n"
    )


def _extension(runtime: str) -> str:
    common = _extension_prelude(runtime)
    if runtime == "opencode":
        return (
            common
            + """export const WeoraSkillHooks = async ({ directory }) => ({
  event: async ({ event }) => {
    const props = event.properties ?? {};
    const scope = { cwd: directory, session_id: props.sessionID ?? props.info?.id };
    if (event.type === "session.created") await dispatch("start", scope);
    if (event.type === "session.idle") await dispatch("stop", scope);
  },
  "chat.message": async (input, output) => {
    const prompt = (output.parts ?? []).filter(part => part.type === "text").map(part => part.text).join("\\n");
    await dispatch("prompt", { cwd: directory, session_id: input.sessionID, prompt });
  },
  "tool.execute.after": async (input, output) => {
    const exit = output.metadata?.exit;
    await dispatch("tool", { cwd: directory, session_id: input.sessionID, tool_name: input.tool,
      tool_input: input.args, tool_response: output.output,
      tool_status: typeof exit === "number" ? (exit === 0 ? "success" : "failure") : output.status });
  },
});
"""
        )
    return (
        common
        + """export default function weoraSkillHooks(pi) {
  const inputs = new Map();
  const context = (ctx) => ({ cwd: ctx.cwd, session_id: ctx.sessionManager.getSessionId() });
  pi.on("session_start", async (_event, ctx) => dispatch("start", context(ctx)));
  pi.on("before_agent_start", async (event, ctx) => dispatch("prompt", { ...context(ctx), prompt: event.prompt }));
  pi.on("tool_execution_start", async (event) => inputs.set(event.toolCallId, event.args));
  const toolResult = async (event, ctx) => {
    await dispatch("tool", { ...context(ctx), tool_name: event.toolName,
      tool_input: event.input ?? inputs.get(event.toolCallId),
      tool_response: event.result?.content ?? event.content, tool_status: event.isError ? "failure" : "success" });
    inputs.delete(event.toolCallId);
  };
  pi.on("TOOL_RESULT_EVENT", toolResult);
  pi.on("SESSION_STOP_EVENT", async (_event, ctx) => dispatch("stop", context(ctx)));
}
""".replace("TOOL_RESULT_EVENT", "tool_result" if runtime == "omp" else "tool_execution_end").replace(
            "SESSION_STOP_EVENT",
            "session_stop" if runtime == "omp" else "agent_settled",
        )
    )


def install_hook(runtime: str) -> dict[str, JsonValue]:
    tool = TOOLS[runtime]
    home = tool_home(runtime)
    if tool.hook in {"openclaw", "hermes", "dsh"}:
        path = {"openclaw": _openclaw_hook, "hermes": _hermes_hook, "dsh": _dsh_hook}[tool.hook](home)
    elif tool.hook in {"pi", "opencode"}:
        path = home / ("plugin" if tool.hook == "opencode" else "extensions") / "weora-skill-hooks.ts"
        _write_text(path, _extension(runtime))
    elif tool.hook == "kiro":
        path = home / "hooks/weora-skill-sync.json"
        # TeamAI 先接入启动事件；Kiro CLI V3 文档也定义了其余三个观察入口。
        write_json(
            path,
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": f"weora-skill-{event}",
                        "trigger": name,
                        "enabled": True,
                        "action": {"type": "command", "command": _shell_command(runtime, event)},
                        "timeout": 10,
                    }
                    for name, event in EVENTS.items()
                ],
            },
        )
    elif tool.hook == "agy":
        path = home / "hooks.json"
        config = _settings(path)
        config["weora-skills"] = {
            "SessionStart": [{"type": "command", "command": _shell_command(runtime, "start"), "timeout": 10}],
            "PreInvocation": [{"type": "command", "command": _shell_command(runtime, "prompt"), "timeout": 10}],
            "Stop": [{"type": "command", "command": _shell_command(runtime, "stop"), "timeout": 10}],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": _shell_command(runtime, "tool"), "timeout": 10},
                    ],
                }
            ],
        }
        write_json(path, config)
    else:
        # TRAE 走原生 hooks.json，成员在宿主的 Hook 设置中确认启用。
        name = {"cursor": "hooks.json", "copilot": "hooks/weora-skills.json", "trae": "hooks.json"}.get(
            tool.hook,
            tool.hook,
        )
        path = home / name
        _merge(path, runtime, tool.hook)
    return {"mode": "native", "path": str(path), "runtime": runtime}


def launch(runtime: str, args: list[str], cwd: Path, executable_path: str | None = None) -> int:
    binary = TOOLS[runtime].binary or runtime
    executable = shutil.which(executable_path or binary)
    if not executable:
        raise CollabError("SKILL_RUNTIME_NOT_FOUND", f"没有找到 {binary} 的启动入口。可用 --executable 指定程序。")
    return subprocess.call([executable, *args], cwd=cwd)
