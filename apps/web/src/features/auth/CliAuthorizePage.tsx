import { useMutation, useQuery } from "@tanstack/react-query";
import { Check, Terminal } from "lucide-react";
import { useParams } from "react-router-dom";
import { BrandWordmark } from "../../components/brand/BrandWordmark";
import { Banner, Button, Card, Spin } from "../../components/console";
import { AuthApiError, authWrite } from "./api";
import { useAuth } from "./AuthProvider";
import "./invitation.css";

type LoginRequest = { device_name: string; code: string; user_id: string; workspace_id: string };
type Decision = "approve" | "deny";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch("/api/v1/cli-auth/requests/" + path, { ...init, credentials: "same-origin" });
  if (response.ok) return response.json() as Promise<T>;
  const body = await response.json().catch(() => ({})) as { detail?: { code?: string; message?: string } };
  throw new AuthApiError(body.detail?.message ?? "无法读取登录请求，请重试。", response.status, body.detail?.code);
}

export function CliAuthorizePage() {
  const { loginId = "" } = useParams();
  return <CliAuthorization key={loginId} loginId={loginId} />;
}

function CliAuthorization({ loginId }: { loginId: string }) {
  const { principal } = useAuth();
  const path = encodeURIComponent(loginId);
  const login = useQuery({
    queryKey: ["cli-login", loginId, principal?.user.id, principal?.workspace.id],
    queryFn: () => request<LoginRequest>(path),
    retry: false,
    staleTime: 0,
  });
  const decide = useMutation({
    mutationFn: ({ decision, displayed }: { decision: Decision; displayed: LoginRequest }) => request<{
      status: "approved" | "denied";
    }>(
      path + "/decision",
      authWrite("POST", { decision, user_id: displayed.user_id, workspace_id: displayed.workspace_id }),
    ),
  });
  const error = decide.error ?? login.error;
  const identityChanged = principal && login.data && (
    login.data.user_id !== principal.user.id || login.data.workspace_id !== principal.workspace.id
  );
  const decisionConflict = decide.error instanceof AuthApiError && decide.error.status === 409;
  // 页面身份可能落后于共享 Cookie，账号变化后要求重新确认。
  const readyLogin = principal && !identityChanged && !decisionConflict && !login.isError && login.data;
  const done = decide.data?.status;

  return (
    <main className="invite-public">
      <header className="invite-public-header"><BrandWordmark /></header>
      <div className="invite-public-stage">
        <Card className="invite-public-card">
          <div className="invite-public-stack">
            <div className="invite-public-heading">
              <span className="invite-public-icon" data-tone={done === "approved" ? "success" : undefined}>
                {done === "approved" ? <Check aria-hidden="true" /> : <Terminal aria-hidden="true" />}
              </span>
              <h1>{done === "approved" ? "已授权 Weora CLI" : done === "denied" ? "已拒绝登录" : "登录 Weora CLI"}</h1>
              {done && <p>可以关闭此页面，返回终端。</p>}
              {!done && readyLogin && <p>核对终端显示的验证码，然后确认这台设备。</p>}
            </div>
            {!done && login.isPending && <Spin tip="正在读取登录请求" />}
            {!done && error && <Banner type="danger" description={error.message} />}
            {!done && identityChanged && !error && (
              <Banner type="danger" description="当前登录身份已改变，请刷新授权页。" />
            )}
            {!done && (identityChanged || decisionConflict) && (
              <Button onClick={() => window.location.reload()}>刷新授权页</Button>
            )}
            {!done && readyLogin && principal && <div className="grid gap-4">
              <dl className="grid gap-3">
                <div><dt className="text-sm text-muted">验证码</dt><dd className="font-mono text-xl font-semibold">{readyLogin.code}</dd></div>
                <div><dt className="text-sm text-muted">设备</dt><dd>{readyLogin.device_name}</dd></div>
                <div><dt className="text-sm text-muted">登录身份</dt><dd>{principal.user.display_name} · {principal.workspace.name}</dd></div>
              </dl>
              <p className="text-sm text-muted">
                确认后，这台设备可向当前工作空间提交 Skill 并同步已发布版本。使用统计默认开启，可在「设置 → 本地 Agent」关闭。
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="primary"
                  theme="solid"
                  loading={decide.isPending}
                  onClick={() => decide.mutate({ decision: "approve", displayed: readyLogin })}
                >
                  确认登录
                </Button>
                <Button disabled={decide.isPending} onClick={() => decide.mutate({ decision: "deny", displayed: readyLogin })}>
                  拒绝
                </Button>
              </div>
            </div>}
            {!done && login.isError && !(error instanceof AuthApiError && error.status === 404) && (
              <Button onClick={() => void login.refetch()}>重试</Button>
            )}
          </div>
        </Card>
      </div>
    </main>
  );
}
