"""安装本地客户端扩展，合并用户配置并保留既有 hook。"""

import json
import os
from importlib.resources import files
from pathlib import Path

from weora_cli.collab_client import CollabError, as_object, write_json
from weora_cli.commands import JsonValue


def install(runtime: str, *, auto: bool) -> dict[str, JsonValue]:
    if runtime == "cursor":
        if auto:
            raise CollabError("WAKE_UNAVAILABLE", "Cursor 只能在会话运行时通过 hook 收件，请省略 --auto。")
        return _cursor()
    if not auto:
        raise CollabError("AUTO_OPT_IN_REQUIRED", "安装原生投递扩展需要显式指定 --auto。")
    if runtime == "pi":
        root = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent")) / "extensions"
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        root = Path(os.environ.get("OPENCODE_CONFIG_DIR", config_home / "opencode")) / "plugins"
    plugin = root / "weora-collaboration.ts"
    bridge = root / "weora-collaboration" / "bridge.mjs"
    try:
        assets = files("weora_cli").joinpath("native_plugins")
        # 用独有文件名和标记，更新时不会覆盖用户同名的自建插件。
        for destination in (plugin, bridge):
            if destination.exists() and not destination.read_text(encoding="utf-8").startswith("// Weora managed\n"):
                raise ValueError(destination.name)
        for destination, source in ((bridge, "bridge.mjs"), (plugin, runtime + ".ts")):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(assets.joinpath(source).read_bytes())
    except (OSError, ValueError) as error:
        raise CollabError("HOOK_CONFIG_ERROR", "无法安装 Weora 扩展，请检查文件名冲突和写入权限。") from error
    return {"path": str(plugin), "runtime": runtime, "auto_receive": True}


def _cursor() -> dict[str, JsonValue]:
    path = Path(os.environ.get("WEORA_CURSOR_HOOKS", Path.home() / ".cursor" / "hooks.json"))
    command = "weora collab hook --runtime cursor"
    try:
        config: dict[str, JsonValue] = (
            as_object(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else {"version": 1}
        )
        hooks = as_object(config.setdefault("hooks", {}))
        for event in ("sessionStart", "postToolUse", "stop"):
            rules = hooks.setdefault(event, [])
            if not isinstance(rules, list):
                raise ValueError(event)
            hooks[event] = [rule for rule in rules if as_object(rule).get("command") != command] + [
                {"command": command, "timeout": 8}
            ]
        write_json(path, config)
    except (OSError, ValueError, CollabError) as error:
        raise CollabError("HOOK_CONFIG_ERROR", "无法合并 Cursor hooks.json。") from error
    return {"path": str(path), "runtime": "cursor", "auto_receive": False}
