// Weora managed
import { mkdtempSync, rmdirSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { connectSession } from "./weora-collaboration/bridge.mjs";

export default function (pi) {
  let channel: ReturnType<typeof connectSession> | undefined;
  let activeId: string | undefined;
  let binding: string | undefined;
  let busy = false;
  function close() {
    channel?.close();
    if (!binding) return;
    try { unlinkSync(join(binding, "session.json")); } catch { /* 尚未登记时没有绑定文件。 */ }
    try { rmdirSync(binding); } catch { /* 只移除本次创建的空目录。 */ }
    binding = undefined;
  }
  function attach(ctx) {
    const id = ctx.sessionManager.getSessionId();
    if (activeId === id && channel?.active) return;
    close();
    activeId = id;
    const name = ctx.sessionManager.getSessionName() || "Pi " + id.slice(0, 8);
    binding = mkdtempSync(join(tmpdir(), "weora-pi-"));
    const file = join(binding, "session.json");
    channel = connectSession({
      runtime: "pi", id, name, cwd: ctx.cwd,
      registered(sessionId) {
        if (activeId !== id) return;
        writeFileSync(file, JSON.stringify({ session_id: sessionId }), { mode: 0o600 });
        if (typeof pi.registerMcpServer !== "function") return;
        pi.registerMcpServer("weora", {
          command: process.env.WEORA_CLI_EXECUTABLE || "weora",
          args: ["collab", "mcp", "--runtime", "pi", "--name", name, "--binding-file", file],
          env: process.env.WEORA_COLLAB_CONFIG ? { WEORA_COLLAB_CONFIG: process.env.WEORA_COLLAB_CONFIG } : {},
          exposure: "direct",
        });
      },
      deliver(text) {
        // 切换会话后，旧子进程的延迟提示不能进入新会话。
        if (activeId !== id || ctx.sessionManager.getSessionId() !== id) return "failed";
        const hint = typeof pi.registerMcpServer === "function"
          ? "Use the Weora MCP claim, reply and ack tools if available. " + text : text;
        pi.sendUserMessage(hint, busy ? { deliverAs: "followUp" } : undefined);
        return "accepted";
      },
    });
  }
  pi.on("session_start", (_event, ctx) => attach(ctx));
  pi.on("session_switch", (_event, ctx) => attach(ctx));
  pi.on("agent_start", (_event, ctx) => { busy = true; attach(ctx); });
  pi.on("agent_end", () => { busy = false; });
  pi.on("agent_settled", () => { busy = false; });
  pi.on("session_shutdown", () => {
    activeId = undefined;
    close();
  });
}
