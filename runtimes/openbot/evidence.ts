import type { BrowserContext, Page, Request } from "playwright";

type RequestFact = { method: string; url: string; status?: number; outcome: "response" | "failed" | "blocked" };
type Trace = { facts: RequestFact[]; generation: number };
const records = new WeakMap<BrowserContext, Trace>();

function safeUrl(raw: string): string {
  const url = new URL(raw);
  return url.protocol === "http:" || url.protocol === "https:" ? url.origin + url.pathname : "";
}

function remember(facts: RequestFact[], fact: RequestFact): void {
  if (!fact.url) return;
  // 保留业务请求，静态图片与字体不挤占故障证据。查询参数和请求体不会进入事实记录。
  facts.push(fact);
  if (facts.length > 64) facts.shift();
}

export async function observeBrowser(page: Page, origins: unknown): Promise<void> {
  const context = page.context();
  const previous = records.get(context);
  if (previous) {
    previous.generation += 1;
    previous.facts.length = 0;
    return;
  }
  const trace: Trace = { facts: [], generation: 0 };
  const facts = trace.facts;
  const generation = new WeakMap<Request, number>();
  records.set(context, trace);
  // 迟到的请求属于原导航，不能污染随后复现步骤的证据。
  context.on("request", (request) => generation.set(request, trace.generation));
  context.on("response", (response) => {
    const request = response.request();
    if (generation.get(request) === trace.generation && ["document", "xhr", "fetch"].includes(request.resourceType())) {
      remember(facts, { method: request.method(), url: safeUrl(response.url()), status: response.status(), outcome: "response" });
    }
  });
  context.on("requestfailed", (request) => {
    if (generation.get(request) === trace.generation && ["document", "xhr", "fetch"].includes(request.resourceType())) {
      remember(facts, { method: request.method(), url: safeUrl(request.url()), outcome: "failed" });
    }
  });

  if (origins === undefined) return;
  const allowed = new Set(Array.isArray(origins) ? origins.filter((item) => typeof item === "string") : []);
  await context.routeWebSocket("**/*", (socket) => socket.close());
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!allowed.has(url.origin) || !["GET", "HEAD"].includes(request.method())) {
      remember(facts, { method: request.method(), url: safeUrl(url.href), outcome: "blocked" });
      return route.abort();
    }
    try {
      // 不让 Playwright 自动跟随重定向。公开检查使用明确的最终 URL。
      const response = await route.fetch({ maxRedirects: 0, timeout: 10000 });
      if (response.status() >= 300 && response.status() < 400) return await route.abort();
      await route.fulfill({ response });
    } catch {
      await route.abort().catch(() => undefined);
    }
  });
}

export async function browserEvidence(page: Page) {
  const facts = records.get(page.context())?.facts ?? [];
  let screenshot = await page.screenshot({ timeout: 8000, scale: "css" });
  const viewport = page.viewportSize();
  if (screenshot.length > 190000 && viewport) {
    // 画面过大时保留首屏局部，避免截断编码后生成损坏的证据。
    screenshot = await page.screenshot({
      timeout: 8000, scale: "css",
      clip: { x: 0, y: 0, width: Math.min(640, viewport.width), height: Math.min(360, viewport.height) },
    });
  }
  return {
    url: safeUrl(page.url()), title: await page.title(), requests: facts.slice(),
    screenshot: screenshot.length <= 190000 ? screenshot.toString("base64") : null,
  };
}
