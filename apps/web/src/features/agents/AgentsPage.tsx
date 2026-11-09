import type { TableColumn } from "../../components/console/data-table";
import { Bot as IconAIFilledLevel2, Plus as IconPlus } from "lucide-react";
import {
  Banner,
  Button,
  Empty,
  Table,
  Tag,
  Typography,
} from "../../components/console";
import {
  AssetDetailsLink,
  AssetLoadError,
} from "../../components/console/asset-controls";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { formatDate } from "../skills/SkillsPage";
import { AgentEditorDialog } from "./AgentEditorDialog";
import {
  AgentsApiError,
  createAgent,
  listAgents,
  type AgentSummary,
  type CreateAgentInput,
} from "./api";

export function AgentsPage() {
  const { principal } = useAuth();
  const queryClient = useQueryClient();
  const [editorOpen, setEditorOpen] = useState(false);
  if (!principal) throw new Error("Agent 页面缺少当前工作空间");

  const workspaceId = principal.workspace.id;
  const canWrite = ["owner", "admin"].includes(principal.workspace.role);
  const agentsQuery = useQuery({
    queryKey: ["agents", workspaceId],
    queryFn: () => listAgents(workspaceId),
  });
  const createMutation = useMutation({
    mutationFn: (input: CreateAgentInput) => createAgent(workspaceId, input),
    onSuccess: (created) => {
      queryClient.setQueryData<{ items: AgentSummary[] }>(
        ["agents", workspaceId],
        (current) => ({ items: [created, ...(current?.items ?? [])] }),
      );
      setEditorOpen(false);
    },
  });
  const items = agentsQuery.data?.items ?? [];
  const columns: TableColumn<AgentSummary>[] = [
    {
      title: "Agent",
      dataIndex: "display_name",
      render: (_: unknown, agent: AgentSummary) => (
        <Link className="resource-cell" to={`/agents/${agent.id}`}>
          <AgentLogo />
          <span>
            <strong>{agent.display_name}</strong>
            <small>{agent.description}</small>
          </span>
        </Link>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (status) => <AgentStatusTag status={status} />,
    },
    {
      title: "最新版本",
      dataIndex: "latest_published_version",
      width: 150,
      render: (value: string | null) => (
        <Tag color={value ? "blue" : "grey"}>{value ?? "仅草稿"}</Tag>
      ),
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 160,
      render: (value: string) => (
        <time dateTime={value}>{formatDate(value)}</time>
      ),
    },
    {
      title: "",
      dataIndex: "id",
      width: 106,
      render: (_: unknown, agent: AgentSummary) => (
        <AssetDetailsLink
          to={`/agents/${agent.id}`}
          name={agent.display_name}
        />
      ),
    },
  ];

  return (
    <section className="asset-page asset-mobile-list page-enter">
      <header className="page-header">
        <div>
          <span className="page-eyebrow">AGENT REGISTRY</span>
          <Typography.Title heading={1}>Agent</Typography.Title>
          <Typography.Paragraph>
            管理 Prompt、固定 SkillVersion 绑定和不可变发布版本。
          </Typography.Paragraph>
        </div>
        {canWrite ? (
          <Button
            aria-label="创建 Agent"
            icon={<IconPlus />}
            onClick={() => setEditorOpen(true)}
            size="large"
            theme="solid"
            type="primary"
          >
            创建 Agent
          </Button>
        ) : null}
      </header>

      <Banner
        className="boundary-banner"
        closeIcon={null}
        description="设置提示词并绑定团队 Skill，发布固定版本后即可在任务中使用。修改草稿不会影响已经发布的版本。"
        title="从配置到协作"
        type="info"
      />

      <section className="asset-surface">
        {agentsQuery.isError ? (
          <AssetLoadError
            description={
              agentsQuery.error instanceof AgentsApiError
                ? agentsQuery.error.message
                : "请检查网络连接后重试。"
            }
            title="Agent 暂时无法加载"
            onRetry={() => void agentsQuery.refetch()}
          />
        ) : null}

        {!agentsQuery.isPending &&
        !agentsQuery.isError &&
        items.length === 0 ? (
          <div className="asset-empty">
            <Empty
              description="创建 Agent 草稿，并为它选择明确的已发布 Skill 版本。"
              image={<IconAIFilledLevel2 size={32} />}
              title="还没有 Agent"
            />
            {canWrite ? (
              <Button
                aria-label="创建第一个 Agent"
                onClick={() => setEditorOpen(true)}
                theme="light"
                type="primary"
              >
                创建第一个 Agent
              </Button>
            ) : null}
          </div>
        ) : null}

        {agentsQuery.isPending || items.length > 0 ? (
          <Table
            className="asset-table asset-stack-table"
            variant="secondary"
            columns={columns}
            dataSource={items}
            loading={agentsQuery.isPending}
            pagination={false}
            rowKey="id"
          />
        ) : null}
      </section>

      <AgentEditorDialog
        onClose={() => setEditorOpen(false)}
        onSubmit={(input) => createMutation.mutateAsync(input)}
        open={editorOpen}
        workspaceId={workspaceId}
      />
    </section>
  );
}

export function AgentStatusTag({ status }: { status: AgentSummary["status"] }) {
  return (
    <Tag color={status === "active" ? "green" : "grey"}>
      {status === "active" ? "活跃" : "已归档"}
    </Tag>
  );
}

export function AgentLogo({ large = false }: { large?: boolean }) {
  return (
    <span
      className={large ? "resource-mark agent large" : "resource-mark agent"}
      aria-hidden="true"
    >
      <IconAIFilledLevel2 />
    </span>
  );
}
