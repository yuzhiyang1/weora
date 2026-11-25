"""把与后端共用的纯 Python 提取器装入可独立安装的 CLI。"""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        name = "weora_code_graph"
        bundled = root / "src" / name
        shared = bundled if bundled.is_dir() else root.parent.parent / "backend" / "src" / name
        # sdist 保留同一份源码，使解包后重新构建 wheel 不依赖 Weora 仓库。
        target = f"src/{name}" if self.target_name == "sdist" else name
        build_data["force_include"][str(shared)] = target
