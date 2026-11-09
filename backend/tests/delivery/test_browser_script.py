"""执行真实 Node 验证脚本的分支单测；浏览器接口替身不冒充真实 Chromium 集成。"""

import asyncio
import json
import os
import shutil
from uuid import UUID, uuid4

import pytest

from weora.modules.delivery.application.ports import SandboxResult
from weora.modules.delivery.infrastructure.browser import SandboxBrowserVerifier
from weora.modules.delivery.infrastructure.docker import DockerSandbox

NODE = shutil.which("node")
pytestmark = [pytest.mark.anyio, pytest.mark.skipif(NODE is None, reason="需要本机 Node 执行浏览器脚本分支")]

MOCK = r"""
const Module=require('node:module');
const original=Module._load;
const mode=MODE;
let route,websocket,proxyOptions;
const external={request:()=>({url:()=> 'http://192.0.2.10:8080/blocked'}),abort:async()=>{},continue:async()=>{}};
const page={
 routeWebSocket:async(pattern,handler)=>{websocket=handler},
 on:()=>{},
 goto:async()=>{
  if(mode==='proxy-redirect'){
   const proxy=new URL(proxyOptions.server);
   await new Promise((resolve,reject)=>{
    const req=require('node:http').request({hostname:proxy.hostname,port:proxy.port,
     path:'http://127.0.0.1:3001/redirected'},res=>{
      res.resume();res.on('end',()=>res.statusCode===403?resolve():reject(new Error('not denied')));
     });req.on('error',reject);req.end();
   });throw new Error('redirect blocked by transport');
  }
  if(mode==='redirect'){await route(external);throw new Error('navigation blocked')}
  if(mode==='websocket'){await websocket({close:async()=>{}})}
  return {status:()=>mode==='404'?404:200};
 },
 url:()=> 'http://127.0.0.1:3000/ok',
 locator:()=>({innerText:async()=> 'expected body'}),
 screenshot:async()=>{
  if(mode==='late'){await route(external)}
  return Buffer.from('89504e470d0a1a0a','hex');
 },
};
const context={route:async(pattern,handler)=>{route=handler},routeWebSocket:async(pattern,handler)=>{websocket=handler},
 on:()=>{},newPage:async()=>page};
Module._load=function(name,...args){
 if(name==='/opt/weora-browser/node_modules/playwright'){
  return {chromium:{launch:async()=>({newContext:async options=>{proxyOptions=options.proxy;return context},
   close:async()=>{}})}};
 }
 return original.call(this,name,...args);
};
"""


class ScriptDocker(DockerSandbox):
    """仅替代 Chromium，不替代待验证的正式 Node 控制流。"""

    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def _exec(
        self, run_id: UUID, *argv: str, stdin: bytes | None = None, output_limit: int | None = None
    ) -> SandboxResult:
        assert NODE
        script = MOCK.replace("MODE", json.dumps(self.mode)) + argv[-1]
        env = {key: value for key, value in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR"}}
        process = await asyncio.create_subprocess_exec(
            NODE,
            "-e",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            async with asyncio.timeout(5):
                stdout, stderr = await process.communicate(stdin)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        return SandboxResult("PASSED", stdout.decode(), 0)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("ok", "PASSED"),
        ("late", "URL_DENIED"),
        ("redirect", "URL_DENIED"),
        ("proxy-redirect", "URL_DENIED"),
        ("websocket", "URL_DENIED"),
        ("404", "SERVICE_NOT_READY"),
    ],
)
async def test_browser_script_verdict_does_not_hide_blocked_or_failed_navigation(mode: str, expected: str) -> None:
    result = await SandboxBrowserVerifier(ScriptDocker(mode), service_port=3000).verify(uuid4(), "/ok", "expected body")
    assert result.status == expected
