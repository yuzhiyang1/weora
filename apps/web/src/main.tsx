import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./styles/global.css";

// 登录页与工作台共用已保存的外观，首次访问使用本次选定的深色基线。
const theme =
  localStorage.getItem("weora-appearance") === "light" ? "light" : "dark";
document.documentElement.classList.toggle("dark", theme === "dark");
document.documentElement.dataset.theme = theme;

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("缺少前端根节点 #root，无法启动 Weora");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
