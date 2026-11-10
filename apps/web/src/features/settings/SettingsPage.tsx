import type { PropsWithChildren, ReactNode } from "react";
import { Banner, Button, Spin, Tag } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { errorMessage } from "../runs/api";
import "./settings.css";

export function SettingsPage({
  title,
  description,
  scope = "业务空间级",
  actions,
  headingAction,
  children,
}: PropsWithChildren<{
  title: string;
  description: string;
  scope?: string;
  actions?: ReactNode;
  headingAction?: ReactNode;
}>) {
  return (
    <div className="settings-page">
      <header
        className={
          headingAction
            ? "settings-heading settings-heading-with-action"
            : "settings-heading"
        }
      >
        <div>
          {headingAction ? (
            <div className="settings-heading-title">
              <h1>{title}</h1>
              <Tag>{scope}</Tag>
            </div>
          ) : (
            <h1>{title}</h1>
          )}
          <p>{description}</p>
        </div>
        {headingAction || <Tag>{scope}</Tag>}
      </header>
      {actions && <div className="settings-toolbar">{actions}</div>}
      {children}
    </div>
  );
}
export function SettingsResult({
  error,
  success,
}: {
  error?: unknown;
  success?: string | null;
}) {
  return (
    <>
      {!!error && <Banner type="danger" description={errorMessage(error)} />}
      {success && <Banner type="success" description={success} />}
    </>
  );
}
export function SettingsLoading({
  loading,
  error,
  retry,
}: {
  loading: boolean;
  error: unknown;
  retry: () => unknown;
}) {
  if (loading) return <Spin tip="正在加载设置…" />;
  if (!error) return null;
  return (
    <div className="settings-stack">
      <SettingsResult error={error} />
      <Button onClick={() => void retry()}>重新加载</Button>
    </div>
  );
}
export function useTeamManager() {
  const { principal } = useAuth();
  return (
    principal?.workspace.role === "owner" ||
    principal?.workspace.role === "admin"
  );
}
