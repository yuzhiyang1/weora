import "./host.css";

// 比较器只管理预览切换，iframe 将两套组件库的全局样式彻底隔离。
const query = new URLSearchParams(location.search);
let variant = query.get("variant") === "shadcn" ? "shadcn" : "heroui";
let theme = query.get("theme") === "light" ? "light" : "dark";
const root = document.getElementById("comparison")!;
root.innerHTML = `
  <iframe title="HeroUI 独立交互预览" data-variant="heroui" src="/heroui.html?theme=${theme}"></iframe>
  <iframe title="shadcn/ui 独立交互预览" data-variant="shadcn" src="/shadcn.html?theme=${theme}"></iframe>
  <nav class="comparison-bar" aria-label="UI 方案对比">
    <span class="comparison-label">交互预览</span>
    <button data-select="heroui">A · HeroUI</button>
    <button data-select="shadcn">B · shadcn/ui</button>
    <span class="bar-divider"></span>
    <button id="theme" aria-label="切换浅色主题">☀</button>
    <button id="reset" title="重置当前演示" aria-label="重置当前演示">↺</button>
    <a id="standalone" target="_blank" rel="noreferrer">单独打开 ↗</a>
  </nav>`;
const frames = Array.from(
  document.querySelectorAll<HTMLIFrameElement>("iframe"),
);
function render() {
  frames.forEach((frame) => {
    frame.hidden = frame.dataset.variant !== variant;
  });
  document
    .querySelectorAll<HTMLButtonElement>("[data-select]")
    .forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.select === variant),
      ),
    );
  query.set("variant", variant);
  query.set("theme", theme);
  history.replaceState(null, "", `/?${query}`);
  const open = document.getElementById("standalone") as HTMLAnchorElement;
  open.href = `/${variant}.html?theme=${theme}`;
  const toggle = document.getElementById("theme")!;
  toggle.textContent = theme === "dark" ? "☀" : "☾";
  toggle.setAttribute(
    "aria-label",
    theme === "dark" ? "切换浅色主题" : "切换深色主题",
  );
}
document
  .querySelectorAll<HTMLButtonElement>("[data-select]")
  .forEach((button) =>
    button.addEventListener("click", () => {
      variant = button.dataset.select!;
      render();
    }),
  );
document.getElementById("theme")!.addEventListener("click", () => {
  theme = theme === "dark" ? "light" : "dark";
  frames.forEach((frame) =>
    frame.contentWindow?.postMessage(
      { type: "weora-preview-theme", theme },
      location.origin,
    ),
  );
  render();
});
document
  .getElementById("reset")!
  .addEventListener("click", () => {
    const frame = frames.find((item) => item.dataset.variant === variant);
    // 重置演示数据时保留当前主题，仅重新加载正在查看的方案。
    if (frame) frame.src = `/${variant}.html?theme=${theme}`;
  });
window.addEventListener("message", (event) => {
  if (
    event.origin !== location.origin ||
    !frames.some((frame) => frame.contentWindow === event.source)
  )
    return;
  if (
    event.data?.type === "weora-preview-theme" &&
    event.data.theme !== theme
  ) {
    theme = event.data.theme === "light" ? "light" : "dark";
    frames.forEach((frame) => {
      if (frame.contentWindow !== event.source)
        frame.contentWindow?.postMessage(
          { type: "weora-preview-theme", theme },
          location.origin,
        );
    });
    render();
  }
});
window.addEventListener("keydown", (event) => {
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    variant = variant === "heroui" ? "shadcn" : "heroui";
    render();
  }
});
render();
