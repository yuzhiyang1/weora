"""构建期校验公开浏览器缓存，再由原版 Playwright 安装器完成安装；不进入运行沙箱。"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import cast


def verified_routes(cache: Path, registry: dict[str, object]) -> dict[str, Path]:
    """缺少清单时走官方在线安装；存在清单则必须全量匹配当前驱动版本及摘要，失败不降级。"""
    manifest = cache / "manifest.json"
    if not manifest.exists():
        return {}
    records = json.loads(manifest.read_text(encoding="utf-8"))
    descriptors = cast(list[dict[str, str]], registry["browsers"])
    routes: dict[str, Path] = {}
    for name, filename in (
        ("chromium-headless-shell", "chrome-headless-shell-linux64.zip"),
        ("ffmpeg", "ffmpeg-linux.zip"),
    ):
        descriptor = next(item for item in descriptors if item["name"] == name)
        record = records[name]
        path = cache / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("浏览器缓存缺失或不是常规文件")
        if record["revision"] != descriptor["revision"] or record["filename"] != filename:
            raise ValueError("浏览器缓存与当前 Playwright revision 不匹配")
        digest = record["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("浏览器缓存摘要无效")
        if not 0 < path.stat().st_size <= 268435456 or path.stat().st_size != record["size"]:
            raise ValueError("浏览器缓存大小不符或超限")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("浏览器缓存 SHA256 不匹配")
        if name == "chromium-headless-shell":
            version = descriptor["browserVersion"]
            if record["browser_version"] != version:
                raise ValueError("浏览器缓存版本不匹配")
            route = f"/builds/cft/{version}/linux64/{filename}"
        else:
            route = f"/builds/ffmpeg/{descriptor['revision']}/{filename}"
        routes[route] = path
    return routes


def install(cache: Path, modules: Path) -> None:
    """仅在镜像构建时开放回环静态文件服务；不代理外网、不改变安装器或生成完成标记。"""
    registry = json.loads((modules / "playwright-core/browsers.json").read_text(encoding="utf-8"))
    routes = verified_routes(cache, registry)
    command = ["node", str(modules / "playwright/cli.js"), "install", "--only-shell", "chromium"]
    if not routes:
        subprocess.run(command, check=True, timeout=570)
        return

    class CacheHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            path = routes.get(self.path)
            if path is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)

    server = ThreadingHTTPServer(("127.0.0.1", 0), CacheHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.pop("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", None)
    env["PLAYWRIGHT_DOWNLOAD_HOST"] = f"http://127.0.0.1:{server.server_port}"
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1"
    try:
        subprocess.run(command, env=env, check=True, timeout=570)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    install(Path("/opt/weora-browser-cache"), Path("/opt/weora-browser/node_modules"))
