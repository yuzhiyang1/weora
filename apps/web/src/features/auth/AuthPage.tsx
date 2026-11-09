import {
  PanelsTopLeft as IconApps,
  EyeOff as IconEyeClosed,
  Eye as IconEyeOpened,
  LockKeyhole as IconLock,
  Mail as IconMail,
  ShieldCheck as IconShield,
} from "lucide-react";
import { Banner, Button, Input, Typography } from "../../components/console";
import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { AuthApiError } from "./api";
import { useAuth } from "./AuthProvider";
import { BrandWordmark } from "../../components/brand/BrandWordmark";
import "./auth.css";

type AuthMode = "login" | "register";

export function AuthPage({ mode }: { mode: AuthMode }) {
  const isRegister = mode === "register";
  const { login, register } = useAuth();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);

    const form = new FormData(event.currentTarget);
    try {
      if (isRegister) {
        await register({
          display_name: String(form.get("display_name")),
          email: String(form.get("email")),
          password: String(form.get("password")),
          workspace_name: String(form.get("workspace_name")),
        });
      } else {
        await login({
          email: String(form.get("email")),
          password: String(form.get("password")),
        });
      }

      // 登录状态更新后由 GuestOnly 统一返回原页面，表单不再发起第二次跳转。
    } catch (requestError) {
      setError(
        requestError instanceof AuthApiError
          ? requestError.message
          : "暂时无法连接服务，请稍后再试",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-story" aria-label="Weora 产品介绍">
        <Link className="auth-brand" to="/" aria-label="Weora Agent operations">
          <span className="auth-brand-copy">
            <BrandWordmark />
            <small>Agent operations</small>
          </span>
        </Link>
        <div className="auth-story-copy">
          <span className="page-eyebrow">ENTERPRISE AGENT PLATFORM</span>
          <h2>把个人的 Agent 实践，变成团队可治理的能力。</h2>
          <p>
            连接不同模型和 Runtime，同时保留版本、权限、审批、审计与恢复边界。
          </p>
        </div>
        <div className="auth-principles">
          <span>
            <IconShield />
            工作空间隔离
          </span>
          <span>
            <IconLock />
            不可变发布版本
          </span>
          <span>
            <IconApps />多 Runtime 接入
          </span>
        </div>
      </section>

      <section className="auth-form-stage">
        <div className="auth-form-wrap">
          <div className="auth-heading">
            <span className="page-eyebrow">
              {isRegister ? "CREATE WORKSPACE" : "WELCOME BACK"}
            </span>
            <Typography.Title heading={2}>
              {isRegister ? "创建 Weora 账户" : "登录 Weora"}
            </Typography.Title>
            <Typography.Paragraph>
              {isRegister
                ? "创建账户，并建立你的第一个团队工作空间。"
                : "使用工作邮箱进入团队控制面。"}
            </Typography.Paragraph>
          </div>

          <form className="auth-form" onSubmit={handleSubmit}>
            {isRegister ? (
              <label className="auth-field" htmlFor="display_name">
                <span>你的称呼</span>
                <Input
                  aria-label="你的称呼"
                  id="display_name"
                  name="display_name"
                  autoComplete="name"
                  maxLength={80}
                  placeholder="例如：张晨"
                  size="large"
                  required
                />
              </label>
            ) : null}

            <label className="auth-field" htmlFor="email">
              <span>工作邮箱</span>
              <Input
                aria-label="工作邮箱"
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                placeholder="name@company.com"
                prefix={<IconMail />}
                size="large"
                required
              />
            </label>

            {isRegister ? (
              <label className="auth-field" htmlFor="workspace_name">
                <span>团队工作空间</span>
                <Input
                  aria-label="团队工作空间"
                  id="workspace_name"
                  name="workspace_name"
                  autoComplete="organization"
                  minLength={2}
                  maxLength={120}
                  placeholder="例如：产品研发中心"
                  size="large"
                  required
                />
              </label>
            ) : null}

            <label className="auth-field" htmlFor="password">
              <span>密码</span>
              <Input
                aria-label="密码"
                id="password"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete={isRegister ? "new-password" : "current-password"}
                minLength={isRegister ? 15 : 1}
                maxLength={128}
                placeholder={isRegister ? "至少 15 个字符" : "输入密码"}
                prefix={<IconLock />}
                size="large"
                suffix={
                  <Button
                    aria-label={showPassword ? "隐藏密码" : "显示密码"}
                    icon={showPassword ? <IconEyeClosed /> : <IconEyeOpened />}
                    onClick={() => setShowPassword((current) => !current)}
                    theme="borderless"
                    type="tertiary"
                  />
                }
                required
              />
              {/* 前端不能假定后端开启密码绕过，登录提示始终遵循真实账户语义。 */}
              <small className="auth-hint">
                {isRegister
                  ? "建议使用一句容易记住、但他人难以猜到的长密码。"
                  : "请输入该账户的登录密码。"}
              </small>
            </label>

            {error ? (
              <Banner closeIcon={null} description={error} type="danger" />
            ) : null}

            <Button
              block
              htmlType="submit"
              loading={isSubmitting}
              size="large"
              theme="solid"
              type="primary"
            >
              {isSubmitting
                ? isRegister
                  ? "正在创建"
                  : "正在登录"
                : isRegister
                  ? "创建账户并进入"
                  : "登录"}
            </Button>
          </form>

          <p className="auth-switch">
            {isRegister ? "已有账户？" : "还没有账户？"}
            <Link to={isRegister ? "/login" : "/register"}>
              {isRegister ? "去登录" : "创建账户"}
            </Link>
          </p>
          <footer className="auth-footer">
            继续即表示你同意遵守所在组织的 AI 使用与数据安全规范。
          </footer>
        </div>
      </section>
    </main>
  );
}
