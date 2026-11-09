import { BrandWordmark } from "../../components/brand/BrandWordmark";
import { Spin } from "../../components/console";
import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "./AuthProvider";

export function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") return <AuthLoading />;
  if (status === "guest") {
    return <Navigate replace state={{ from: location }} to="/login" />;
  }
  return <Outlet />;
}

export function GuestOnly({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();
  const pathname = (location.state as { from?: { pathname?: unknown } } | null)
    ?.from?.pathname;
  // 由路由守卫统一导航，避免表单提交与 Navigate 竞争，且不接受外站或认证页循环跳转。
  const destination =
    typeof pathname === "string" &&
    pathname.startsWith("/") &&
    !pathname.startsWith("//") &&
    !pathname.includes("\\") &&
    !["/login", "/register"].includes(pathname.replace(/\/+$/, ""))
      ? pathname
      : "/";

  if (status === "loading") return <AuthLoading />;
  if (status === "authenticated") return <Navigate replace to={destination} />;
  return children;
}

function AuthLoading() {
  return (
    <main className="auth-loading" aria-label="正在恢复登录状态">
      <BrandWordmark />
      <Spin size="large" tip="正在进入 Weora" />
    </main>
  );
}
