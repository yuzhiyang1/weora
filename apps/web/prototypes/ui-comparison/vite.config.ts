import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

// 独立 HTML 入口确保两套主题和组件 CSS 不进入同一个文档。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Windows 的外置依赖存储也需要预构建，保证 CommonJS 依赖可被浏览器加载。
  optimizeDeps: {
    include: [
      "@heroui/react",
      "react-aria-components",
      "react",
      "react-dom/client",
    ],
  },
  build: {
    rollupOptions: {
      input: {
        comparison: resolve(import.meta.dirname, "index.html"),
        heroui: resolve(import.meta.dirname, "heroui.html"),
        shadcn: resolve(import.meta.dirname, "shadcn.html"),
      },
    },
  },
});
