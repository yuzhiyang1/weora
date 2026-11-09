/** 桌面和 Chromium 共享 OpenBot 创建的 Xvfb 显示。 */
export function startDesktop(display: string): void {
  const desktop = Bun.spawn(["dbus-run-session", "--", "xfce4-session"], {
    env: { ...process.env, DISPLAY: display, COMPUTER_TOKEN: undefined },
    stdout: "ignore",
    stderr: "inherit",
  });
  void desktop.exited.then((code) => {
    console.error("Desktop session exited:", code);
    process.exit(1);
  });
}

export async function desktopScreen(display: string) {
  const capture = Bun.spawn(
    ["timeout", "3", "import", "-display", display, "-window", "root", "-quality", "78", "jpeg:-"],
    { stdout: "pipe", stderr: "ignore" },
  );
  const data = await new Response(capture.stdout).arrayBuffer();
  if ((await capture.exited) !== 0 || !data.byteLength || data.byteLength > 1_000_000) {
    throw new Error("Desktop capture unavailable");
  }
  return { base64: Buffer.from(data).toString("base64"), width: 1280, height: 800 };
}

const specialKeys = /^(Return|Tab|Escape|BackSpace|Delete|space|Left|Right|Up|Down|Home|End|Page_Up|Page_Down)$/;

export function desktopInput(display: string, input: unknown): boolean {
  if (!input || typeof input !== "object") return false;
  const { key, x, y, button, clicks } = input as Record<string, unknown>;
  let args: string[];
  if (typeof key === "string" && (/^[\x21-\x7e]$/.test(key) || specialKeys.test(key))) {
    args = key.length === 1 ? ["type", "--", key] : ["key", key];
  } else if (
    typeof x === "number" && Number.isInteger(x) && x >= 0 && x < 1280 &&
    typeof y === "number" && Number.isInteger(y) && y >= 0 && y < 800 &&
    (button === 1 || button === 3 || button === 4 || button === 5) &&
    (clicks === 1 || clicks === 2)
  ) {
    args = ["mousemove", String(x), String(y), "click", "--repeat", String(clicks), String(button)];
  } else {
    return false;
  }
  // 有界同步输入确保交还请求不会穿过尚未完成的人工键鼠动作。
  return Bun.spawnSync(["timeout", "1", "xdotool", ...args], {
    env: { DISPLAY: display, PATH: process.env.PATH },
    stdout: "ignore",
    stderr: "ignore",
  }).exitCode === 0;
}
