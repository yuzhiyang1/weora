import { Check, Clock3, UserRound, UsersRound } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { buttonVariants } from "@heroui/styles";
import { BrandWordmark } from "../../components/brand/BrandWordmark";
import { Banner, Button, Card, Spin } from "../../components/console";
import { AuthApiError } from "./api";
import { AuthPage } from "./AuthPage";
import { useAuth } from "./AuthProvider";
import { previewInvitation, type InvitePreview } from "./workspace-api";
import "./invitation.css";

type PreviewResult = { token: string; invite?: InvitePreview; error?: unknown };

export function InvitationPage() {
  const { hash } = useLocation();
  // 使用片段承载凭据，避免浏览器把邀请发送到 Web 服务器的访问日志。
  const token = /^#[a-f0-9]{32}\.[a-f0-9]{64}$/.test(hash) ? hash.slice(1) : "";
  const navigate = useNavigate();
  const { status, principal, acceptInvite, logout } = useAuth();
  const [result, setResult] = useState<PreviewResult>();
  const [refresh, setRefresh] = useState(0);
  const [mode, setMode] = useState<"login" | "register" | null>(null);
  const [joinedToken, setJoinedToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    void previewInvitation(token, controller.signal).then(
      (invite) => {
        if (!controller.signal.aborted) setResult({ token, invite });
      },
      (error: unknown) => {
        if (!controller.signal.aborted) setResult({ token, error });
      },
    );
    return () => controller.abort();
  }, [token, refresh]);

  const current = result?.token === token ? result : undefined;
  const invite = current?.invite;
  const unavailable =
    !token ||
    (current?.error instanceof AuthApiError &&
      current.error.code === "INVITE_UNAVAILABLE");
  const joined = Boolean(token) && joinedToken === token;

  function markUnavailable() {
    setResult({
      token,
      error: new AuthApiError("邀请已失效", 410, "INVITE_UNAVAILABLE"),
    });
    setMode(null);
  }

  async function doAction(action: "join" | "switch") {
    if (busy) return;
    setBusy(true);
    setActionError("");
    try {
      if (action === "join") {
        await acceptInvite(token);
        setJoinedToken(token);
      } else {
        await logout();
        setMode("login");
      }
    } catch (cause) {
      if (
        cause instanceof AuthApiError &&
        cause.code === "INVITE_UNAVAILABLE"
      ) {
        markUnavailable();
      } else {
        setActionError(
          cause instanceof AuthApiError
            ? cause.message
            : "暂时无法连接服务，请稍后重试。",
        );
      }
    } finally {
      setBusy(false);
    }
  }

  if (invite && status === "guest" && mode) {
    return (
      <AuthPage
        key={`${token}:${mode}`}
        mode={mode}
        invitation={{
          token,
          workspaceName: invite.workspace_name,
          onModeChange: setMode,
          onJoined: () => setJoinedToken(token),
          onUnavailable: markUnavailable,
        }}
      />
    );
  }

  return (
    <main className="invite-public">
      <header className="invite-public-header">
        <BrandWordmark />
        <span>工作空间邀请</span>
      </header>
      <div className="invite-public-stage">
        <Card className="invite-public-card">
          {joined ? (
            <div className="invite-public-stack">
              <InviteHeading
                icon={<Check />}
                tone="success"
                title="已加入工作空间"
              >
                你现在可以进入{principal?.workspace.name ?? "工作空间"}
                ，查看共享任务并参与协作。
              </InviteHeading>
              <Button
                block
                theme="solid"
                onClick={() => navigate("/", { replace: true })}
              >
                进入工作空间
              </Button>
            </div>
          ) : unavailable ? (
            <div className="invite-public-stack">
              <InviteHeading
                icon={<Clock3 />}
                tone="warning"
                title="此邀请链接已失效"
              >
                链接可能已过期、被撤销或已被使用。请联系邀请人获取新链接。
              </InviteHeading>
              <Link
                replace
                className={buttonVariants({ variant: "secondary", fullWidth: true })}
                to={principal ? "/" : "/login"}
              >
                {principal ? "返回工作台" : "返回登录"}
              </Link>
            </div>
          ) : current?.error ? (
            <div className="invite-public-stack">
              <Banner
                type="danger"
                description={
                  current.error instanceof AuthApiError
                    ? current.error.message
                    : "暂时无法读取邀请。"
                }
              />
              <Button
                onClick={() => {
                  setResult(undefined);
                  setRefresh((value) => value + 1);
                }}
              >
                重新加载
              </Button>
            </div>
          ) : !invite || status === "loading" ? (
            <Spin tip="正在读取邀请…" />
          ) : (
            <div className="invite-public-stack">
              <InviteHeading
                icon={<UsersRound />}
                title={`加入「${invite.workspace_name}」`}
              >
                {invite.inviter_name}邀请你以普通成员身份加入该工作空间。
              </InviteHeading>
              {principal ? (
                <>
                  <div className="invite-account">
                    <UserRound size={18} aria-hidden="true" />
                    <div>
                      <small>当前账号</small>
                      <strong>
                        {principal.user.display_name} · {principal.user.email}
                      </strong>
                    </div>
                  </div>
                  {actionError && (
                    <Banner type="danger" description={actionError} />
                  )}
                  <Button
                    block
                    theme="solid"
                    loading={busy}
                    onClick={() => void doAction("join")}
                  >
                    加入工作空间
                  </Button>
                  <Button
                    block
                    theme="borderless"
                    disabled={busy}
                    onClick={() => void doAction("switch")}
                  >
                    切换账号
                  </Button>
                </>
              ) : (
                <>
                  <Button block theme="solid" onClick={() => setMode("login")}>
                    登录后加入
                  </Button>
                  <div className="invite-register">
                    还没有账号？
                    <Button
                      theme="borderless"
                      onClick={() => setMode("register")}
                    >
                      注册并加入
                    </Button>
                  </div>
                </>
              )}
            </div>
          )}
        </Card>
      </div>
    </main>
  );
}

function InviteHeading({
  icon,
  title,
  tone = "accent",
  children,
}: {
  icon: React.ReactNode;
  title: string;
  tone?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="invite-public-heading">
      <span className="invite-public-icon" data-tone={tone} aria-hidden="true">
        {icon}
      </span>
      <h1>{title}</h1>
      <p>{children}</p>
    </div>
  );
}
