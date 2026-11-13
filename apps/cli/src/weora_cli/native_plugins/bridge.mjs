// Weora managed
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

export function connectSession({ runtime, id, name, cwd, deliver, registered }) {
  // 子进程只服务当前原生会话。路径由本机环境指定，不从协作服务读取。
  const child = spawn(process.env.WEORA_CLI_EXECUTABLE || "weora", [
    "collab", "session-bridge", "--runtime", runtime, "--native-id", id, "--name", name,
  ], { cwd, stdio: ["pipe", "pipe", "ignore"], windowsHide: true });
  let active = true;
  const lines = createInterface({ input: child.stdout });
  child.on("error", () => { active = false; });
  child.on("exit", () => { active = false; });
  child.stdin.on("error", () => { active = false; });
  lines.on("line", async (line) => {
    if (!active) return;
    let event;
    try { event = JSON.parse(line); } catch { return; }
    if (event.event === "registered") {
      registered?.(event.session_id);
      return;
    }
    if (event.event !== "wake" || typeof event.hint !== "string") return;
    let state = "unknown";
    try { state = await deliver(event.hint); } catch {
      // 回执异常时保留结果未知，由收件箱等候主动领取。
    }
    if (active) child.stdin.write(JSON.stringify({ id: event.id, state }) + "\n");
  });
  return {
    get active() { return active; },
    close() {
      active = false;
      lines.close();
      child.stdin.end();
      child.unref();
    },
  };
}
