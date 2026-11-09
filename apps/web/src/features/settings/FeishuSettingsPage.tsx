import { useState } from "react";
import { Button, Card, Tabs, TabPane } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { ContextConfiguration } from "../context/ConnectionsPage";
import { ChannelStatusPanel } from "../context/ChannelStatusPanel";
import { useSettingsEnvironment } from "./SettingsEnvironment";
import { SettingsPage, useTeamManager } from "./SettingsPage";

export function FeishuSettingsPage() {
  const { principal } = useAuth();
  const manager = useTeamManager();
  const env = useSettingsEnvironment();
  const [tab, setTab] = useState("apps");
  const [routing, setRouting] = useState(false);
  return (
    <SettingsPage
      title="飞书连接"
      description="连接、群来源、读者授权和成员身份分别管理。"
      scope="团队级"
    >
      <Tabs
        className="settings-feishu-tabs"
        activeKey={tab}
        onChange={(value) => env?.attempt(() => setTab(value))}
      >
        <TabPane itemKey="apps" tab="应用与来源" />
        <TabPane itemKey="auth" tab="授权与身份" />
      </Tabs>
      <ContextConfiguration
        key={`${tab}:${routing}`}
        workspaceId={principal!.workspace.id}
        userId={principal!.user.id}
        manager={manager}
        view={tab as "apps" | "auth"}
      />
      {manager && tab === "apps" && (
        <>
          <Card
            title="群路由与投递"
            headerExtraContent={
              <Button
                onClick={() =>
                  env?.attempt(() => setRouting((value) => !value))
                }
              >
                {routing ? "收起高级配置" : "配置路由与查看状态"}
              </Button>
            }
          >
            <p className="form-help">
              设置显式 @ 请求的执行入口，查看消息接收与回帖状态。自动接单策略在
              Agent 协调中管理。
            </p>
          </Card>
          {routing && (
            <ChannelStatusPanel
              workspaceId={principal!.workspace.id}
              userId={principal!.user.id}
            />
          )}
        </>
      )}
    </SettingsPage>
  );
}
