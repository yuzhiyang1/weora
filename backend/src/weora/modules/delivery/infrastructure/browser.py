"""浏览器在同一无网络沙箱运行，只允许该 Run 容器的指定 localhost 服务。"""

import base64
import binascii
import json
from uuid import UUID

from weora.modules.delivery.application.ports import BrowserResult
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.docker import DockerSandbox


class SandboxBrowserVerifier:
    def __init__(self, sandbox: DockerSandbox, *, service_port: int | None = None) -> None:
        self.sandbox = sandbox
        self.service_port = service_port

    async def verify(self, run_id: UUID, path: str, expected_text: str) -> BrowserResult:
        if self.service_port is None:
            return BrowserResult("NOT_CONFIGURED", "尚未配置沙箱浏览器与服务端口")
        if not 1024 <= self.service_port <= 65535:
            raise DeliveryError("BROWSER_NOT_CONFIGURED", "浏览器服务端口配置无效")
        if not path.startswith("/") or path.startswith("//") or "\\" in path or len(path) > 2048:
            raise DeliveryError("BROWSER_URL_DENIED", "浏览器只接受当前 Run 服务的相对路径")
        if not expected_text or len(expected_text) > 2000:
            raise DeliveryError("INVALID_ASSERTION", "请指定有界的页面文本断言")
        origin = f"http://127.0.0.1:{self.service_port}"
        # network=none 禁止其他主机网络；route 再限制 localhost 端口和重定向。
        script = r"""
const fs=require('node:fs');
const http=require('node:http');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{
 let browser,guard;
 let denied=false;
 try {
  const {chromium}=require('/opt/weora-browser/node_modules/playwright');
  // Playwright route 只拦截重定向链首个 URL。容器内回环转发器逐跳核验真实目标，
  // 不是宿主下载代理；既不开放外网，也不把同源重定向改写成伪造的最终页面。
  guard=http.createServer((request,response)=>{
   let target;
   try{target=new URL(request.url);}catch{denied=true;response.writeHead(403);response.end();return;}
   if(target.origin!==input.origin || target.protocol!=='http:' || target.username || target.password){
    denied=true;response.writeHead(403);response.end();return;
   }
   const headers={...request.headers,host:target.host,connection:'close'};
   delete headers['proxy-authorization'];delete headers['proxy-connection'];
   const upstream=http.request(target,{method:request.method,headers,agent:false},incoming=>{
    response.writeHead(incoming.statusCode||502,incoming.headers);incoming.pipe(response);
    incoming.on('error',()=>response.destroy());
   });
   upstream.setTimeout(10000,()=>upstream.destroy());
   upstream.on('error',()=>{if(!response.headersSent)response.writeHead(502);response.end();});
   request.on('aborted',()=>upstream.destroy());request.on('error',()=>upstream.destroy());
   response.on('close',()=>upstream.destroy());request.pipe(upstream);
  });
  guard.on('connect',(_request,socket)=>{denied=true;socket.end('HTTP/1.1 403 Forbidden\r\n\r\n');});
  await new Promise((resolve,reject)=>{guard.once('error',reject);guard.listen(0,'127.0.0.1',resolve);});
  browser=await chromium.launch({headless:true});
  // 显式取消 Chromium 的隐式 loopback 直连例外，所有页面/重定向都经过同一策略。
  const context=await browser.newContext({serviceWorkers:'block',acceptDownloads:false,
   proxy:{server:`http://127.0.0.1:${guard.address().port}`,bypass:'<-loopback>'}});
  await context.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.origin!==input.origin || !['http:'].includes(url.protocol)){
    denied=true; return route.abort('blockedbyclient');
   }
   return route.continue();
  });
  // 所有页面/弹窗共享策略；下载或 WebSocket 尝试不能在成功截图中被隐去。
  context.on('page',child=>child.on('download',download=>{
   denied=true; download.cancel().catch(()=>{});
  }));
  await context.routeWebSocket('**/*',socket=>{denied=true;socket.close();});
  const page=await context.newPage();
  const response=await page.goto(input.origin+input.path,{waitUntil:'domcontentloaded',timeout:15000});
  if(denied || new URL(page.url()).origin!==input.origin){
   console.log(JSON.stringify({status:'URL_DENIED',report:'检测到越界跳转或资源请求'})); return;
  }
  if(!response || response.status()<200 || response.status()>=300){
   console.log(JSON.stringify({status:'SERVICE_NOT_READY',report:'沙箱服务未就绪'})); return;
  }
  const text=await page.locator('body').innerText({timeout:5000});
  const passed=text.includes(input.expected);
  const shot=await page.screenshot({type:'png',fullPage:false});
  // 截图可能等待字体/布局；期间才触发的越界请求同样必须覆盖最终判断。
  if(denied || new URL(page.url()).origin!==input.origin){
   console.log(JSON.stringify({status:'URL_DENIED',report:'检测到越界跳转、资源或通道请求'}));return;
  }
  if(shot.length>196608){console.log(JSON.stringify({status:'SCREENSHOT_FAILED',report:'截图超过预算'}));return;}
  console.log(JSON.stringify({status:passed?'PASSED':'ASSERTION_FAILED',
   report:passed?'页面文本断言通过':'页面文本断言失败',screenshot_base64:shot.toString('base64')}));
 }catch(error){
  if(denied){
   console.log(JSON.stringify({status:'URL_DENIED',report:'检测到越界跳转、资源或通道请求'}));return;
  }
  const code=error && error.code;
  console.log(JSON.stringify({status:code==='MODULE_NOT_FOUND'?'NOT_CONFIGURED':'SERVICE_NOT_READY',
   report:code==='MODULE_NOT_FOUND'?'沙箱镜像未安装浏览器':'服务不可访问、浏览器未就绪或验证超时'}));
 }finally{
  if(browser)await browser.close();
  if(guard){guard.closeAllConnections();await new Promise(resolve=>guard.close(resolve));}
 }
})().catch(()=>process.exitCode=1);
"""
        payload = json.dumps({"origin": origin, "path": path, "expected": expected_text}).encode()
        result = await self.sandbox._exec(run_id, "node", "-e", script, stdin=payload, output_limit=300000)
        if result.status == "TIMEOUT":
            return BrowserResult("TIMEOUT", "浏览器验证超时，沙箱已停止")
        # stdout 中的 JSON 不能覆盖进程失败事实，避免崩溃前输出被误判为验收通过。
        if result.status != "PASSED" or result.exit_code != 0:
            return BrowserResult("SERVICE_NOT_READY", "浏览器验证进程未成功完成")
        try:
            data = json.loads(result.output)
            statuses = {
                "PASSED",
                "ASSERTION_FAILED",
                "URL_DENIED",
                "SERVICE_NOT_READY",
                "NOT_CONFIGURED",
                "SCREENSHOT_FAILED",
            }
            if data.get("status") not in statuses or not isinstance(data.get("report"), str):
                raise ValueError
            shot = data.get("screenshot_base64")
            # 成功断言必须同时具有截图证据；空输出不能作为真实浏览器验收。
            if data["status"] == "PASSED" and not shot:
                raise ValueError
            if shot is not None and (not isinstance(shot, str) or len(shot) > 262144):
                raise ValueError
            if shot and not base64.b64decode(shot, validate=True).startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError
            return BrowserResult(data["status"], data["report"], shot)
        except (ValueError, AttributeError, TypeError, binascii.Error):
            return BrowserResult("SCREENSHOT_FAILED", "浏览器未返回有效验证证据")
