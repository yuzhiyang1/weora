import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // 限制并行 JSDOM 数量，避免本机同时运行后端与浏览器时争抢内存。
    maxWorkers: 4,
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
