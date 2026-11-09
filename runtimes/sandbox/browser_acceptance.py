"""通过真实 DockerSandbox 验收容器浏览器，所有页面均来自本脚本的无凭据夹具。"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.browser import SandboxBrowserVerifier
from weora.modules.delivery.infrastructure.docker import (
    DockerConfig,
    DockerSandbox,
    RepositorySnapshot,
)

SERVER = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time,json,threading
blocked_port_hits=0
class Trap(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  global blocked_port_hits
  blocked_port_hits+=1
  self.send_response(200);self.end_headers();self.wfile.write(b'Weora W09 Browser')
threading.Thread(target=ThreadingHTTPServer(('127.0.0.1',3001),Trap).serve_forever,daemon=True).start()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.path=='/redirect-local':
   self.send_response(302);self.send_header('Location','/ok');self.end_headers();return
  if self.path=='/redirect-chain':
   self.send_response(302);self.send_header('Location','/redirect-port');self.end_headers();return
  if self.path=='/redirect-https':
   self.send_response(302);self.send_header('Location','https://127.0.0.1:3001/out');self.end_headers();return
  if self.path=='/guard-hits':
   self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'blocked_port_hits':blocked_port_hits}).encode());return
  if self.path in ('/redirect-external','/redirect-port'):
   target='http://192.0.2.10:8080/out' if self.path.endswith('external') else 'http://127.0.0.1:3001/out'
   self.send_response(302); self.send_header('Location',target); self.end_headers(); return
  if self.path=='/slow-font.woff':
   time.sleep(.3); self.send_response(200); self.end_headers(); self.wfile.write(b'not-a-font'); return
  script=''
  if self.path=='/external': script="<img src='http://192.0.2.10:8080/pixel'>"
  if self.path=='/late-external':
   script="<style>@font-face{font-family:late;src:url('/slow-font.woff')}body{font-family:late}</style><script>setTimeout(()=>fetch('http://192.0.2.10:8080/late').catch(()=>{}),50)</script>"
  if self.path=='/websocket': script="<script>new WebSocket('ws://127.0.0.1:3001/socket')</script>"
  self.send_response(404 if self.path=='/not-found' else 200)
  self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers()
  body=("<!doctype html><meta charset='utf-8'><style>body{background:#f5f9ff;color:#175cd3;"
        "padding:48px;font:24px sans-serif}</style><h1>Weora W09 Browser</h1>"
        "<p>Container-only verification fixture.</p>")
  self.wfile.write((body+script).encode())
ThreadingHTTPServer(('127.0.0.1',3000),Handler).serve_forever()
"""


async def accept(image: str, output: Path) -> None:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    archive_path = output / "input.tar"
    with tarfile.open(archive_path, "w") as archive:
        body = SERVER.encode()
        entry = tarfile.TarInfo("server.py")
        entry.size, entry.mode = len(body), 0o644
        archive.addfile(entry, io.BytesIO(body))
    archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    config = DockerConfig(
        image,
        {"browser-acceptance": RepositorySnapshot(archive_path.resolve(), "b" * 40, archive_hash)},
        executable="wsl.exe",
        executable_args=(
            "-d",
            "Ubuntu",
            "-u",
            "root",
            "--exec",
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "--config",
            "/var/lib/weora/docker-cli",
        ),
        deployment_id="weora-browser-acceptance-" + uuid4().hex,
        timeout_seconds=60,
        memory_mb=512,
    )
    sandbox, run_id = DockerSandbox(config), uuid4()
    scope = PlanScope("browser-acceptance", "b" * 40, ("server.py",), (), ("browser.verify",))
    facts: dict[str, object] = {
        "image": image,
        "run_id": str(run_id),
        "archive_sha256": archive_hash,
    }
    results: list[dict[str, object]] = []
    facts["cases"] = results
    failures: list[str] = []
    try:
        await sandbox.prepare(run_id, scope)
        code, raw = await sandbox._cli("inspect", "--format", "{{json .}}", sandbox.name(run_id))
        assert code == 0
        inspected = json.loads(raw)
        host = inspected["HostConfig"]
        assert inspected["Config"]["User"] == "10001:10001" and host["ReadonlyRootfs"]
        assert host["NetworkMode"] == "none" and "ALL" in host["CapDrop"]
        assert not host["Binds"] and not host["PortBindings"]
        facts["isolation"] = {
            "user": "10001:10001",
            "network": "none",
            "readonly_root": True,
            "cap_drop": "ALL",
        }
        versions = await sandbox.command(
            run_id,
            (
                "node",
                "-e",
                (
                    "(async()=>{const p=require('/opt/weora-browser/node_modules/playwright');"
                    "const b=await p.chromium.launch({headless:true});"
                    "console.log(JSON.stringify({node:process.versions.node,"
                    "playwright:require('/opt/weora-browser/node_modules/playwright/package.json').version,"
                    "browser_version:b.version()}));await b.close()})().catch(()=>process.exitCode=1)"
                ),
            ),
        )
        assert versions.status == "PASSED", versions.output
        facts["versions"] = json.loads(versions.output)
        assert json.loads(versions.output)["playwright"] == "1.64.1"
        started = await sandbox.command(
            run_id,
            (
                "python3",
                "-c",
                (
                    "import subprocess;subprocess.Popen(['python3','server.py'],stdin=subprocess.DEVNULL,"
                    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"
                ),
            ),
        )
        assert started.status == "PASSED", started.output
        # 明确等端口就绪，不把宿主网络或宽权限作为启动浏览器的捷径。
        ready = await sandbox.command(
            run_id,
            (
                "python3",
                "-c",
                (
                    "import socket,time\nfor _ in range(50):\n try:\n"
                    "  socket.create_connection(('127.0.0.1',3000),timeout=.2).close();break\n"
                    " except OSError: time.sleep(.1)\nelse: raise RuntimeError('fixture server not ready')"
                ),
            ),
        )
        assert ready.status == "PASSED", ready.output
        verifier = SandboxBrowserVerifier(sandbox, service_port=3000)
        cases = [
            ("ok", "/ok", "Weora W09 Browser", "PASSED"),
            ("assertion-failed", "/ok", "This text does not exist", "ASSERTION_FAILED"),
            ("redirect-local", "/redirect-local", "Weora W09 Browser", "PASSED"),
            ("external", "/external", "Weora W09 Browser", "URL_DENIED"),
            ("late-external", "/late-external", "Weora W09 Browser", "URL_DENIED"),
            (
                "redirect-external",
                "/redirect-external",
                "Weora W09 Browser",
                "URL_DENIED",
            ),
            ("redirect-port", "/redirect-port", "Weora W09 Browser", "URL_DENIED"),
            ("redirect-chain", "/redirect-chain", "Weora W09 Browser", "URL_DENIED"),
            ("redirect-https", "/redirect-https", "Weora W09 Browser", "URL_DENIED"),
            ("websocket-port", "/websocket", "Weora W09 Browser", "URL_DENIED"),
            ("not-found", "/not-found", "Weora W09 Browser", "SERVICE_NOT_READY"),
        ]
        for name, path, text, expected in cases:
            result = await verifier.verify(run_id, path, text)
            item: dict[str, object] = {
                "case": name,
                "status": result.status,
                "expected": expected,
                "report": result.report,
            }
            results.append(item)
            if result.screenshot_base64:
                (output / (name + ".png")).write_bytes(base64.b64decode(result.screenshot_base64))
            if result.status != expected:
                failures.append(name)
            print(json.dumps(item, ensure_ascii=False), flush=True)
        for path in ("https://192.0.2.10/", "//127.0.0.1:3001/", "/\\evil"):
            try:
                await verifier.verify(run_id, path, "Weora")
            except DeliveryError as error:
                assert error.code == "BROWSER_URL_DENIED"
            else:
                failures.append("untrusted-input-url")
        wrong_port = await SandboxBrowserVerifier(sandbox, service_port=3002).verify(run_id, "/", "Weora")
        results.append(
            {
                "case": "no-service-port",
                "status": wrong_port.status,
                "expected": "SERVICE_NOT_READY",
            }
        )
        if wrong_port.status != "SERVICE_NOT_READY":
            failures.append("no-service-port")
        # 监听真实的被禁端口，证明在建立越界请求前拒绝，不只是连不上后再报错。
        trap = await sandbox.command(
            run_id,
            (
                "python3",
                "-c",
                "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:3000/guard-hits').read().decode())",
            ),
        )
        assert trap.status == "PASSED"
        facts["blocked_port_probe"] = json.loads(trap.output)
        assert json.loads(trap.output)["blocked_port_hits"] == 0, trap.output
    finally:
        await sandbox.cleanup(run_id)
        facts["failures"] = failures
        (output / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not failures, failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(accept(arguments.image, arguments.output))
