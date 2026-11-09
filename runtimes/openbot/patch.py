"""仅对锁定的 OpenBot 源码接入桌面与逐电脑凭据。"""

import hashlib
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

root = Path(sys.argv[1])


def replace(path: str, anchor: str, value: str) -> None:
    target = root / path
    source = target.read_text(encoding="utf-8")
    if source.count(anchor) != 1:
        raise SystemExit(f"OpenBot 锚点已变化：{path}")
    target.write_text(source.replace(anchor, value), encoding="utf-8")


env = "supervisor/src/environment.ts"
replace(env, "/**", 'import { createHmac } from "node:crypto";\n\n/**')
replace(
    env,
    "...(computerToken ? [`COMPUTER_TOKEN=${computerToken}`] : []),",
    '...(computerToken ? [`COMPUTER_TOKEN=${createHmac("sha256", computerToken)'
    '.update("weora-computer:" + botId).digest("hex")}`] : []),',
)
service = "agent-computer/src/index.ts"
replace(
    service,
    'import { startVirtualDisplay } from "./virtual-display";',
    'import { startVirtualDisplay } from "./virtual-display";\n'
    'import { desktopScreen, desktopInput, startDesktop } from "./desktop";\n'
    'import { browserEvidence, observeBrowser } from "./evidence";',
)
replace(
    service,
    "if (VIRTUAL_DISPLAY) process.env.DISPLAY = VIRTUAL_DISPLAY.name;",
    """if (!VIRTUAL_DISPLAY) throw new Error("Weora desktop requires headed mode");
process.env.DISPLAY = VIRTUAL_DISPLAY.name;
startDesktop(VIRTUAL_DISPLAY.name);""",
)
replace(
    service,
    '      if (url.pathname === "/control" && request.method === "GET") {',
    """      if (["/exec", "/computers/reset", "/computers/stop", "/files/write"].includes(url.pathname)) {
        return json({ error: "This operation is disabled in Weora." }, 403);
      }
      if (url.pathname === "/evidence" && request.method === "GET") {
        return json(await browserEvidence(await currentPage(botId)));
      }
      if (url.pathname === "/credential" && request.method === "POST") {
        const body = await request.json().catch(() => null) as {
          ref?: unknown; snapshotId?: unknown; text?: unknown; origin?: unknown; field?: unknown;
        } | null;
        if (!body || typeof body.ref !== "string" || typeof body.snapshotId !== "number" ||
            typeof body.text !== "string" || body.text.length > 1024 ||
            typeof body.origin !== "string" || !["username", "password"].includes(String(body.field))) {
          return json({ error: "Invalid credential request." }, 400);
        }
        const target = await currentPage(botId);
        // 来源校验与填充在同一控制屏障内完成，重定向后不能把凭据填入另一站点。
        if (new URL(target.url()).origin !== body.origin) return json({ error: "Origin changed." }, 409);
        const field = await resolveRef(session, target, body.ref, body.snapshotId);
        if (await field.evaluate(node => node.ownerDocument.location.origin) !== body.origin) {
          return json({ error: "Credential frame origin mismatch." }, 409);
        }
        const type = await field.getAttribute("type");
        if (body.field === "password" && type?.toLowerCase() !== "password") {
          return json({ error: "Password field required." }, 409);
        }
        await field.evaluate(node => node.setAttribute("data-weora-sensitive", "true"));
        await field.fill(body.text, { timeout: ACTION_TIMEOUT_MS });
        session.snapshotId += 1;
        return json({ filled: true });
      }
      if (url.pathname === "/desktop/screen" && request.method === "GET") {
        return json(await desktopScreen(VIRTUAL_DISPLAY!.name));
      }
      if (url.pathname === "/desktop/input" && request.method === "POST") {
        const body: unknown = await request.json().catch(() => null);
        // 解析结束后再检查控制权，输入在同一个事件循环内完成。
        const controlId = (body as { control_id?: string } | null)?.control_id;
        if (!controlId || !session.control.humanMayDrive() || session.control.get().request?.id !== controlId) {
          return json({ error: "Take control first." }, 409);
        }
        if (!desktopInput(VIRTUAL_DISPLAY!.name, body)) return json({ error: "Invalid input." }, 400);
        return json({ ok: true });
      }
      if (url.pathname === "/control" && request.method === "GET") {""",
)
# 快照也占用 Agent 操作屏障，接管完成后不能读取人工正在输入的页面。
replace(
    service,
    "      if (actsOnTheComputer(url.pathname)) {",
    '      if (["/snapshot", "/evidence", "/read", "/credential"].includes(url.pathname) || actsOnTheComputer(url.pathname)) {',
)
replace(service, "          mutatesBrowser(url.pathname),", '          url.pathname === "/credential" || mutatesBrowser(url.pathname),')
replace(
    service,
    '          const target = await currentPage(botId, "navigate");',
    '          const target = await currentPage(botId, "navigate");\n'
    '          await observeBrowser(target, (body as { observeOrigins?: unknown })?.observeOrigins);',
)
replace(
    service,
    '  const yaml = await target.ariaSnapshot({ mode: "ai" });',
    '''  const yaml = await target.ariaSnapshot({ mode: "ai" });
  const parsed = parseAriaSnapshot(yaml);
  for (const element of parsed.elements) {
    if (element.role !== "textbox" || element.value === undefined) continue;
    // ARIA 快照包含密码原值，必须在电脑服务返回前删除；失效的引用也不回传值。
    const sensitive = await target.locator(`aria-ref=${element.ref}`).evaluate((node) => {
      const type = node.getAttribute("type")?.toLowerCase();
      const autocomplete = node.getAttribute("autocomplete")?.toLowerCase().split(/\\s+/) ?? [];
    return node.hasAttribute("data-weora-sensitive") || type === "password" || autocomplete.some((value) =>
        ["current-password", "new-password", "one-time-code"].includes(value));
    }).catch(() => true);
    if (sensitive) delete element.value;
  }''',
)
replace(service, "    ...parseAriaSnapshot(yaml),", "    ...parsed,")
# detached clone 的 innerText 会退化为 textContent，将隐藏登录弹窗误报为可见正文。
replace(
    service,
    '''    const clone = document.body?.cloneNode(true) as HTMLElement | undefined;
    if (!clone) return "";
    for (const node of clone.querySelectorAll("script, style, noscript, svg")) {
      node.remove();
    }
    return clone.innerText ?? "";''',
    '    return document.body?.innerText ?? "";',
)
# 普通 span/div 也可能是业务操作入口。保留文字引用供点击，并优先保留原有语义控件。
aria = "agent-computer/src/aria-snapshot.ts"
replace(
    aria,
    '  if (!INTERACTIVE_ROLES.has(descriptor.role)) return null;',
    '''  const textControl = descriptor.role === "generic" && typeof value === "string" && !!value.trim();
  if (!INTERACTIVE_ROLES.has(descriptor.role) && !textControl) return null;''',
)
replace(
    aria,
    '    name: cutAtCodeUnits(descriptor.name, 200),',
    '    name: cutAtCodeUnits(textControl ? descriptor.name || String(value).trim() : descriptor.name, 200),',
)
replace(
    aria,
    '  const elements: SnapshotElement[] = [];',
    '  const elements: SnapshotElement[] = [];\n  const textElements: SnapshotElement[] = [];',
)
replace(
    aria,
    '  const push = (element: SnapshotElement): void => {',
    '''  const push = (element: SnapshotElement): void => {
    if (element.role === "generic") {
      if (textElements.length < SNAPSHOT_ELEMENT_LIMIT) textElements.push(element);
      return;
    }''',
)
replace(
    aria,
    '  walk(tree);\n  return { elements, truncated };',
    '''  walk(tree);
  const remaining = SNAPSHOT_ELEMENT_LIMIT - elements.length;
  elements.push(...textElements.slice(0, remaining));
  return { elements, truncated: truncated || textElements.length > remaining };''',
)
replace(
    service,
    '          // The page is the Bot\'s working surface, so a failed navigation is reported rather than',
    '          if ((body as { observeOrigins?: unknown })?.observeOrigins !== undefined) {\n'
    '            return json({ navigation_failed: true, code: "BROWSER_NAVIGATION_FAILED" });\n'
    '          }\n'
    '          // The page is the Bot\'s working surface, so a failed navigation is reported rather than',
)
replace(
    "agent-computer/src/profiles.ts",
    "          viewport: VIEWPORT,",
    '          viewport: VIEWPORT,\n          serviceWorkers: "block",',
)
dockerfile_path = "agent-computer/Dockerfile"
dockerfile = root / dockerfile_path
archive_path = sys.argv[2] if len(sys.argv) > 2 else ""
if archive_path:
    # Chromium 151.0.7922.34 对应锁定的 Playwright 1.62.1 revision 1234。
    archive = Path(archive_path)
    if archive.stat().st_size != 193_282_658:
        raise SystemExit("Chromium 公开归档大小不匹配")
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != "ae8736ac28bc69278551500f219fc749575648263c43ec5990749eff43b9fcf8":
        raise SystemExit("Chromium 公开归档摘要不匹配")
    dest = root / "weora-browser"
    with ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = PurePosixPath(member.filename)
            if path.parts[0] != "chrome-linux64" or ".." in path.parts:
                raise SystemExit("Chromium 公开归档目录无效")
            extracted = Path(bundle.extract(member, dest))
            if not member.is_dir():
                extracted.chmod((member.external_attr >> 16) & 0o777 or 0o644)
    (dest / "INSTALLATION_COMPLETE").touch()
    replace(dockerfile_path, "install --with-deps chromium", "install-deps chromium")
    replace(
        dockerfile_path,
        "WORKDIR /app",
        "COPY weora-browser/ /ms-playwright/chromium-1234/\nWORKDIR /app",
    )

with dockerfile.open("a", encoding="utf-8") as output:
    output.write(
        "\nRUN apt-get update && apt-get install -y --no-install-recommends "
        "xvfb xfce4-session xfwm4 xfce4-panel xfdesktop4 thunar dbus-x11 xdotool imagemagick "
        "fonts-noto-cjk && rm -rf /var/lib/apt/lists/*\n"
        "COPY LICENSE /app/LICENSE.openbot\n"
        "ENV COMPUTER_BROWSER_MODE=headed\n"
    )
