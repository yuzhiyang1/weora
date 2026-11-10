import {
  createContext,
  type PropsWithChildren,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  AuthApiError,
  type AuthPrincipal,
  getCurrentPrincipal,
  login as loginRequest,
  type LoginInput,
  logout as logoutRequest,
  register as registerRequest,
  type RegisterInput,
  saveProfile,
} from "./api";
import {
  acceptInvitation,
  activateWorkspace,
  registerFromInvitation,
  type InviteRegistration,
} from "./workspace-api";

type AuthStatus = "loading" | "authenticated" | "guest";

type AuthContextValue = {
  status: AuthStatus;
  principal: AuthPrincipal | null;
  login: (input: LoginInput) => Promise<void>;
  register: (input: RegisterInput) => Promise<void>;
  registerInvited: (input: InviteRegistration) => Promise<AuthPrincipal>;
  acceptInvite: (token: string) => Promise<AuthPrincipal>;
  switchWorkspace: (id: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshPrincipal: () => Promise<void>;
  updateProfile: (
    name: string,
    avatar?: string | null,
  ) => Promise<AuthPrincipal>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [principal, setPrincipal] = useState<AuthPrincipal | null>(null);

  useEffect(() => {
    let active = true;

    getCurrentPrincipal()
      .then((currentPrincipal) => {
        if (!active) return;
        setPrincipal(currentPrincipal);
        setStatus("authenticated");
      })
      .catch((error) => {
        if (!active) return;
        // 认证恢复失败时保持在登录页，表单提交仍会展示明确的服务端错误。
        if (!(error instanceof AuthApiError) || error.status !== 401) {
          console.error("恢复登录状态失败", error);
        }
        setPrincipal(null);
        setStatus("guest");
      });

    return () => {
      active = false;
    };
  }, []);

  const value = useMemo<AuthContextValue>(() => {
    async function applyPrincipal(next: AuthPrincipal) {
      if (
        principal &&
        (principal.workspace.id !== next.workspace.id ||
          principal.user.id !== next.user.id)
      ) {
        // 活动工作空间保存在会话，切换时同时清掉上一空间的请求与私有缓存。
        await queryClient.cancelQueries();
        queryClient.clear();
      }
      setPrincipal(next);
      setStatus("authenticated");
      return next;
    }
    return {
      status,
      principal,
      login: async (input) => {
        await applyPrincipal(await loginRequest(input));
      },
      register: async (input) => {
        await applyPrincipal(await registerRequest(input));
      },
      registerInvited: async (input) =>
        applyPrincipal(await registerFromInvitation(input)),
      acceptInvite: async (token) =>
        applyPrincipal(await acceptInvitation(token)),
      switchWorkspace: async (id) => {
        await applyPrincipal(await activateWorkspace(id));
      },
      refreshPrincipal: async () => {
        await applyPrincipal(await getCurrentPrincipal());
      },
      updateProfile: async (name, avatar) => {
        // 保存响应已包含完整身份，直接同步，避免第二次读取失败掩盖已成功的写入。
        const saved = await saveProfile(name, avatar);
        setPrincipal(saved);
        return saved;
      },
      logout: async () => {
        await logoutRequest();
        // 同一浏览器可能切换账号；退出后不保留运行、快照及产物的私有缓存。
        await queryClient.cancelQueries();
        queryClient.clear();
        setPrincipal(null);
        setStatus("guest");
      },
    };
  }, [principal, status, queryClient]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth 必须在 AuthProvider 内使用");
  }
  return context;
}
