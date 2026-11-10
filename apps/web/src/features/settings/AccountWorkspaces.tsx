import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, Tag } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { listWorkspaces, roleName } from "../auth/workspace-api";
import { useSettingsEnvironment } from "./SettingsEnvironment";
import { SettingsLoading, SettingsResult } from "./SettingsPage";

export function AccountWorkspaces() {
  const { principal, switchWorkspace } = useAuth();
  const settings = useSettingsEnvironment();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const spaces = useQuery({
    queryKey: [
      "account-workspaces",
      principal?.user.id,
      principal?.workspace.id,
    ],
    queryFn: listWorkspaces,
  });

  async function choose(id: string) {
    setBusy(id);
    setError(null);
    try {
      await switchWorkspace(id);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title="我的工作空间" description="在已加入的团队之间切换。">
      <SettingsLoading
        loading={spaces.isPending}
        error={spaces.error}
        retry={spaces.refetch}
      />
      <SettingsResult error={error} />
      <div className="settings-stack">
        {spaces.data?.map((space) => (
          <div className="settings-row" key={space.id}>
            <div className="settings-inline">
              <strong>{space.name}</strong>
              <Tag>{roleName(space.role)}</Tag>
            </div>
            {space.id === principal?.workspace.id ? (
              <Tag color="blue">当前空间</Tag>
            ) : (
              <Button
                size="small"
                disabled={busy !== null}
                loading={busy === space.id}
                aria-label={`切换到${space.name}`}
                onClick={() => {
                  if (settings) settings.attempt(() => void choose(space.id));
                  else void choose(space.id);
                }}
              >
                切换
              </Button>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
