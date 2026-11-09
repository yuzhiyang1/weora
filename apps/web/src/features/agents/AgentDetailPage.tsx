import type { TableColumn } from "../../components/console/data-table";
import { Pencil as IconEdit, Send as IconSend } from "lucide-react";
import {
  Banner,
  Button,
  Card,
  Modal,
  Skeleton,
  Table,
  Tag,
  TextArea,
  Typography,
} from "../../components/console";
import {
  AssetBackLink,
  AssetLoadError,
} from "../../components/console/asset-controls";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { formatDate } from "../skills/SkillsPage";
import { AgentLogo, AgentStatusTag } from "./AgentsPage";
import {
  AgentsApiError,
  getAgent,
  listAgentVersions,
  publishAgent,
  updateAgentDraft,
  type AgentDetail,
  type AgentVersion,
} from "./api";
import { PublishAgentDialog } from "./PublishAgentDialog";
import { useDiscardChanges } from "../settings/useDiscardChanges";

export function AgentDetailPage() {
  const { principal } = useAuth();
  const { agentId } = useParams<{ agentId: string }>();
  if (!principal || !agentId)
    throw new Error("Agent 详情缺少工作空间或 Agent ID");
  return (
    <AgentDetailContent
      key={`${principal.workspace.id}:${principal.user.id}:${agentId}`}
      workspaceId={principal.workspace.id}
      agentId={agentId}
      canWrite={["owner", "admin"].includes(principal.workspace.role)}
    />
  );
}

function AgentDetailContent({
  workspaceId,
  agentId,
  canWrite,
}: {
  workspaceId: string;
  agentId: string;
  canWrite: boolean;
}) {
  const queryClient = useQueryClient();
  const [publishOpen, setPublishOpen] = useState(false);
  const [draftOpen, setDraftOpen] = useState(false);
  const agentQuery = useQuery({
    queryKey: ["agent", workspaceId, agentId],
    queryFn: () => getAgent(workspaceId, agentId),
  });
  const versionsQuery = useQuery({
    queryKey: ["agent-versions", workspaceId, agentId],
    queryFn: () => listAgentVersions(workspaceId, agentId),
  });
  const saveDraft = useMutation({
    mutationFn: (systemPrompt: string) => {
      const draft = agentQuery.data?.draft;
      if (!draft) throw new Error("Agent 草稿不存在");
      return updateAgentDraft(workspaceId, agentId, {
        system_prompt: systemPrompt,
        skill_version_ids: draft.skills.map((item) => item.skill_version_id),
      });
    },
    onSuccess: (draft) => {
      queryClient.setQueryData<AgentDetail>(
        ["agent", workspaceId, agentId],
        (current) =>
          current
            ? { ...current, draft, updated_at: draft.updated_at }
            : current,
      );
      setDraftOpen(false);
    },
  });
  const publish = useMutation({
    mutationFn: (input: { version: string; changelog: string }) =>
      publishAgent(workspaceId, agentId, input),
    onSuccess: (version) => {
      queryClient.setQueryData<{ items: AgentVersion[] }>(
        ["agent-versions", workspaceId, agentId],
        (current) => ({ items: [version, ...(current?.items ?? [])] }),
      );
      queryClient.setQueryData<AgentDetail>(
        ["agent", workspaceId, agentId],
        (current) =>
          current
            ? { ...current, latest_published_version: version.version }
            : current,
      );
      setPublishOpen(false);
    },
  });

  const backLink = <AssetBackLink to="/agents" label="返回 Agent 列表" />;
  if (agentQuery.isPending) {
    return (
      <section className="detail-page asset-mobile-detail">
        {backLink}
        <Skeleton
          active
          placeholder={<Skeleton.Title style={{ width: "40%" }} />}
        />
      </section>
    );
  }
  if (agentQuery.isError || !agentQuery.data) {
    return (
      <section className="detail-page asset-mobile-detail">
        {backLink}
        <AssetLoadError
          description={errorMessage(agentQuery.error)}
          title="Agent 暂时无法加载"
          onRetry={() => void agentQuery.refetch()}
        />
      </section>
    );
  }

  const agent = agentQuery.data;
  const draft = agent.draft;
  const versions = versionsQuery.data?.items ?? [];
  const versionColumns: TableColumn<AgentVersion>[] = [
    {
      title: "版本",
      dataIndex: "version",
      width: 120,
      render: (value: string) => <Tag color="blue">{value}</Tag>,
    },
    { title: "变更说明", dataIndex: "changelog" },
    {
      title: "Skill 数",
      dataIndex: "skills",
      width: 100,
      render: (value: AgentVersion["skills"]) => value.length,
    },
    {
      title: "发布时间",
      dataIndex: "published_at",
      width: 160,
      render: (value: string) => (
        <time dateTime={value}>{formatDate(value)}</time>
      ),
    },
  ];

  return (
    <section className="detail-page asset-mobile-detail page-enter">
      {backLink}
      <header className="detail-hero">
        <div className="detail-title-block">
          <AgentLogo large />
          <div>
            <span className="page-eyebrow">AGENT DEFINITION</span>
            <Typography.Title heading={1}>
              {agent.display_name}
            </Typography.Title>
            <Typography.Paragraph>{agent.description}</Typography.Paragraph>
          </div>
        </div>
        {canWrite ? (
          <Button
            aria-label="发布版本"
            icon={<IconSend />}
            onClick={() => setPublishOpen(true)}
            size="large"
            theme="solid"
            type="primary"
          >
            发布版本
          </Button>
        ) : null}
      </header>

      <div className="detail-metrics">
        <span>
          <small>状态</small>
          <AgentStatusTag status={agent.status} />
        </span>
        <span>
          <small>最新发布</small>
          <strong>{agent.latest_published_version ?? "暂无"}</strong>
        </span>
        <span>
          <small>更新时间</small>
          <strong>{formatDate(agent.updated_at)}</strong>
        </span>
      </div>

      <div className="detail-columns">
        <Card
          className="detail-card"
          title="草稿配置"
          headerExtraContent={
            canWrite && draft ? (
              <Button
                icon={<IconEdit />}
                onClick={() => {
                  saveDraft.reset();
                  setDraftOpen(true);
                }}
              >
                编辑草稿
              </Button>
            ) : null
          }
        >
          <Typography.Paragraph type="tertiary">
            修改只影响下一次发布，不改变历史版本。
          </Typography.Paragraph>
          <p className="agent-prompt-label">System Prompt</p>
          <pre className="agent-prompt-preview">
            {draft?.system_prompt || "暂无草稿"}
          </pre>
        </Card>

        <Card className="detail-card bindings-card" title="SkillVersion 绑定">
          <Typography.Paragraph type="tertiary">
            运行时只读取这里固定的发布版本。
          </Typography.Paragraph>
          <div className="binding-list">
            {draft?.skills.length ? (
              draft.skills.map((skill) => (
                <div className="binding-row" key={skill.skill_version_id}>
                  <span>
                    <strong>{skill.skill_display_name}</strong>
                    <small>{skill.skill_slug}</small>
                  </span>
                  <Tag color="blue">{skill.version}</Tag>
                </div>
              ))
            ) : (
              <div className="picker-empty">当前草稿没有绑定 Skill。</div>
            )}
          </div>
        </Card>
      </div>

      <Card className="detail-card version-history" title="发布历史">
        <Typography.Paragraph type="tertiary">
          每个版本都保存当时的 Prompt 与 SkillVersion 快照。
        </Typography.Paragraph>
        {versionsQuery.isError ? (
          <AssetLoadError
            title="发布历史暂时无法加载"
            description={errorMessage(versionsQuery.error)}
            onRetry={() => void versionsQuery.refetch()}
          />
        ) : versionsQuery.isPending || versions.length > 0 ? (
          <Table
            className="asset-stack-table"
            variant="secondary"
            columns={versionColumns}
            dataSource={versions}
            loading={versionsQuery.isPending}
            pagination={false}
            rowKey="id"
          />
        ) : (
          <div className="picker-empty">还没有发布版本。</div>
        )}
      </Card>

      {canWrite && draft && draftOpen && (
        <EditAgentDraftDialog
          initialPrompt={draft.system_prompt}
          busy={saveDraft.isPending}
          error={saveDraft.error}
          onClose={() => setDraftOpen(false)}
          onSave={(prompt) => saveDraft.mutate(prompt)}
        />
      )}
      <PublishAgentDialog
        onClose={() => setPublishOpen(false)}
        onSubmit={(input) => publish.mutateAsync(input)}
        open={publishOpen}
      />
    </section>
  );
}

function EditAgentDraftDialog({
  initialPrompt,
  busy,
  error,
  onClose,
  onSave,
}: {
  initialPrompt: string;
  busy: boolean;
  error: Error | null;
  onClose: () => void;
  onSave: (prompt: string) => void;
}) {
  const [prompt, setPrompt] = useState(initialPrompt);
  const { requestClose, confirmation } = useDiscardChanges(
    prompt !== initialPrompt,
    onClose,
  );
  return (
    <>
      <Modal
        title="编辑 Agent 草稿"
        visible
        width={620}
        onCancel={() => {
          if (!busy) requestClose();
        }}
        closeOnEsc={!busy}
        maskClosable={!busy}
      >
        <form
          className="agent-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!busy && prompt.trim()) onSave(prompt.trim());
          }}
        >
          <Typography.Paragraph type="tertiary">
            保存草稿后再发布。已绑定的 SkillVersion 继续使用原固定版本。
          </Typography.Paragraph>
          <label className="form-field">
            <span>System Prompt</span>
            <TextArea
              disabled={busy}
              maxLength={30000}
              onChange={setPrompt}
              rows={7}
              value={prompt}
              required
            />
          </label>
          {error && (
            <Banner
              closeIcon={null}
              type="danger"
              description={errorMessage(error)}
            />
          )}
          <div className="modal-actions">
            <Button disabled={busy} onClick={requestClose}>
              取消
            </Button>
            <Button
              htmlType="submit"
              theme="solid"
              loading={busy}
              disabled={!prompt.trim()}
            >
              保存草稿
            </Button>
          </div>
        </form>
      </Modal>
      {confirmation}
    </>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof AgentsApiError
    ? error.message
    : "请检查网络连接后重试。";
}
