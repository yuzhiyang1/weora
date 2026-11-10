import { defineConfig } from "@rsbuild/core";
import { pluginReact } from "@rsbuild/plugin-react";

const apiProxyTarget =
  process.env.WEORA_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [pluginReact()],
  html: {
    title: "Weora",
    template: "./index.html",
    // 让开发服务和生产构建使用同一份浏览器标签页图标。
    favicon: "./src/components/brand/favicon.svg",
  },
  output: {
    // 随浏览器产物分发实际复用组件的版权与许可声明。
    copy: [{ from: "./licenses", to: "licenses" }],
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: [
      {
        context: ["/api"],
        target: apiProxyTarget,
        changeOrigin: true,
      },
    ],
  },
  source: {
    entry: {
      index: "./src/main.tsx",
    },
  },
});
