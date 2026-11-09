import { useSettingsDirty } from "../settings/SettingsDrafts";
import type { TableColumn } from "../../components/console/data-table";
import {
  Banner,
  Button,
  Card,
  Checkbox,
  Input,
  Select,
  Table,
  Tag,
  Typography,
} from "../../components/console";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { listAgents, listAgentVersions } from "../agents/api";
import { getProject, listProjects, projectKey } from "../projects/api";
import { consoleRequest, errorMessage } from "../runs/api";
import { contextList, contextSave, type Source } from "./api";

type ChannelRecord = {
  id: string;
  run_id?: string;
  status: string;
  message_id: string;
  chat_id?: string;
  thread_id?: string;
  attempts?: number;
  error_code?: string;
  error?: string;
  ingress?: string;
};
type ReceiverStatus = {
  instance_id: string;
  connection_id: string;
  transport_state: string;
  heartbeat_at: string;
  last_callback_at?: string | null;
  last_error_code?: string | null;
  reconnects?: number;
  data_frames?: number;
  last_pong_at?: string | null;
};
type ReceivedEvent = {
  event_id: string;
  event_type: string;
  message_id: string;
  received_at: string;
};
type ChannelStatus = {
  inbox: ChannelRecord[];
  deliveries: ChannelRecord[];
  receivers?: ReceiverStatus[];
  events?: ReceivedEvent[];
};
type Route = {
  id?: string;
  source_id: string;
  agent_id: string;
  agent_version: string;
  thread_id: string | null;
  include_nearby: boolean;
  project_id?: string | null;
  repository_ids?: string[];
};

export function ChannelStatusPanel({
  workspaceId,
  userId,
}: {
  workspaceId: string;
  userId: string;
}) {
  const client = useQueryClient();
  const key = ["context-channels", workspaceId, userId];
  const status = useQuery({
    queryKey: key,
    queryFn: () =>
      consoleRequest<ChannelStatus>(workspaceId, "/context/channels"),
    retry: false,
    refetchInterval: 5000,
  });
  const routes = useQuery({
    queryKey: [...key, "routes"],
    queryFn: () => contextList<Route>(workspaceId, "routes"),
    retry: false,
  });
  const sources = useQuery({
    queryKey: [...key, "sources"],
    queryFn: () => contextList<Source>(workspaceId, "sources?managed=true"),
    retry: false,
  });
  const agents = useQuery({
    queryKey: [...key, "agents"],
    queryFn: () => listAgents(workspaceId),
  });
  const [sourceId, setSourceId] = useState("");
  const [inspectMessage, setInspectMessage] = useState("");
  const [agentId, setAgentId] = useState("");
  const [version, setVersion] = useState("");
  const [threadId, setThreadId] = useState("");
  const [nearby, setNearby] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [repositoryIds, setRepositoryIds] = useState<string[]>([]);
  const [shareProject, setShareProject] = useState(false);
  // 沿用发起任务的项目查询和可搜索多选，不默认把项目分享给群成员。
  const projects = useQuery({
    queryKey: projectKey(workspaceId, userId),
    queryFn: () => listProjects(workspaceId),
  });
  const project = useQuery({
    queryKey: projectKey(workspaceId, userId, projectId),
    queryFn: () => getProject(workspaceId, projectId),
    enabled: !!projectId,
    staleTime: 0,
  });
  const repositories =
    project.data?.repositories.filter((item) => item.status === "active") ?? [];
  const validSelection =
    !projectId ||
    (!project.isPending &&
      !project.isError &&
      project.data?.status === "active" &&
      shareProject &&
      repositoryIds.length > 0 &&
      repositoryIds.every((id) => repositories.some((item) => item.id === id)));
  const versions = useQuery({
    queryKey: [...key, "versions", agentId],
    queryFn: () => listAgentVersions(workspaceId, agentId),
    enabled: !!agentId,
  });
  const draft = JSON.stringify({
    sourceId,
    agentId,
    version,
    threadId,
    nearby,
    projectId,
    repositoryIds,
    shareProject,
  });
  const [savedDraft, setSavedDraft] = useState(draft);
  const submittedDraft = useRef(draft);
  useSettingsDirty(draft !== savedDraft);
  const mutation = useMutation({
    mutationFn: () =>
      contextSave(workspaceId, "routes", {
        source_id: sourceId,
        agent_id: agentId,
        agent_version: version,
        thread_id: threadId.trim() || null,
        include_nearby: nearby,
        ...(projectId
          ? {
              project_id: projectId,
              repository_ids: [...repositoryIds].sort(),
              share_project_with_group: shareProject,
            }
          : {}),
      }),
    onSuccess: () => {
      setSavedDraft(submittedDraft.current);
      void client.invalidateQueries({ queryKey: [...key, "routes"] });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!validSelection || mutation.isPending) return;
    submittedDraft.current = draft;
    mutation.mutate();
  }
  const columns: TableColumn<ChannelRecord>[] = [
    { title: "原消息", dataIndex: "message_id" },
    {
      title: "状态",
      dataIndex: "status",
      render: (value: string) => <Tag>{channelLabel(value)}</Tag>,
    },
    {
      title: "关联运行",
      dataIndex: "run_id",
      render: (value: string | undefined) => value ?? "尚未创建",
    },
    {
      title: "失败代码",
      dataIndex: "error_code",
      render: (value: string | undefined, row: ChannelRecord) =>
        value ?? row.error ?? "—",
    },
  ];
  return (
    <Card>
      <Typography.Title heading={3}>@Weora 路由与渠道状态</Typography.Title>
      <p>
        配置显式 @
        请求的原生路由；普通消息是否成为候选由协调中心的范围与策略决定。
        所有入口仍受身份、来源权限与审批约束。
      </p>
      <section className="receiver-diagnostics" aria-label="飞书接收诊断">
        <Typography.Title heading={4}>接收链路诊断</Typography.Title>
        <p>
          进程心跳、连接状态和消息入站是不同证据；人工补收不计作自动入口通过。
        </p>
        {!(status.data?.receivers ?? []).length && (
          <p>
            当前没有可用的接收进程心跳。可能尚未启用诊断、连接配置已更新，或进程未启动；不能仅据此断言飞书未投递。
          </p>
        )}
        {(status.data?.receivers ?? []).map((receiver) => (
          <article key={receiver.instance_id} className="receiver-status-row">
            <Tag
              color={
                receiver.transport_state === "connected" ? "green" : "grey"
              }
            >
              {(
                {
                  connected: "连接已建立",
                  starting: "正在启动",
                  reconnecting: "正在重连",
                  disconnected: "已断开",
                  stale: "心跳已过期",
                  stopped: "已停止",
                  error: "连接异常",
                } as Record<string, string>
              )[receiver.transport_state] ?? "状态未知"}
            </Tag>
            <span>进程心跳：{receiver.heartbeat_at}</span>
            <span>重连 {receiver.reconnects ?? 0} 次</span>
            <span>业务帧 {receiver.data_frames ?? 0} 条</span>
            <span>网络 PONG：{receiver.last_pong_at ?? "尚无记录"}</span>
            {receiver.last_error_code && (
              <code>{receiver.last_error_code}</code>
            )}
          </article>
        ))}
        <label className="form-field">
          <span>核对本地触发消息</span>
          <Input
            aria-label="核对本地触发消息"
            maxLength={128}
            value={inspectMessage}
            onChange={setInspectMessage}
            placeholder="输入飞书消息 ID，核对事件、Inbox 与 Run"
          />
        </label>
        {inspectMessage.trim() && (
          <div role="status">
            {(() => {
              const id = inspectMessage.trim();
              const inbox = status.data?.inbox.find(
                (row) => row.message_id === id,
              );
              const event = status.data?.events?.find(
                (row) => row.message_id === id,
              );
              if (!inbox && !event)
                return "当前返回的本地记录中未找到此消息；事件列表仅展示最近 50 条，不能据此推断上游投递结果。";
              if (inbox?.ingress === "manual_history")
                return `人工补收 · Inbox ${inbox.status} · ${inbox.run_id ? "已关联 Run " + inbox.run_id : "尚未创建 Run"}；不证明自动接收成功。`;
              if (inbox?.ingress === "compensation")
                return `游标补偿接收 · Inbox ${inbox.status}；不代表实时事件已收到。`;
              return `供应商事件${event ? "已记录" : "未在最近事件中找到"} · ${inbox ? "Inbox " + inbox.status : "尚未创建 Inbox（可能按触发规则忽略）"}${inbox?.run_id ? " · Run " + inbox.run_id : ""}`;
            })()}
          </div>
        )}
        {!!status.data?.events?.length && (
          <details>
            <summary>最近供应商事件（最多 50 条，不含正文）</summary>
            <Table
              rowKey="event_id"
              pagination={false}
              dataSource={status.data.events}
              columns={[
                { title: "事件类型", dataIndex: "event_type" },
                { title: "消息 ID", dataIndex: "message_id" },
                { title: "接收时间", dataIndex: "received_at" },
              ]}
            />
          </details>
        )}
      </section>
      {status.isError || routes.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(status.error ?? routes.error)}
          type="warning"
        />
      ) : null}
      <form className="agent-form" onSubmit={submit}>
        <div className="context-form">
          <div className="form-field">
            <span id="route-source-label">路由群来源</span>
            <Select
              aria-labelledby="route-source-label"
              value={sourceId || undefined}
              optionList={(sources.data?.items ?? []).map((item) => ({
                label: item.name,
                value: item.id,
              }))}
              onChange={(value) => {
                setSourceId(String(value));
                setShareProject(false);
              }}
            />
          </div>
          <div className="form-field">
            <span id="route-agent-label">路由 Agent</span>
            <Select
              aria-labelledby="route-agent-label"
              value={agentId || undefined}
              optionList={(agents.data?.items ?? [])
                .filter((item) => item.latest_published_version)
                .map((item) => ({ label: item.display_name, value: item.id }))}
              onChange={(value) => {
                setAgentId(String(value));
                setVersion("");
              }}
            />
          </div>
          <div className="form-field">
            <span id="route-version-label">路由固定版本</span>
            <Select
              aria-labelledby="route-version-label"
              value={version || undefined}
              disabled={!agentId}
              optionList={(versions.data?.items ?? []).map((item) => ({
                label: item.version,
                value: item.version,
              }))}
              onChange={(value) => setVersion(String(value))}
            />
          </div>
          <label className="form-field">
            <span>限定话题 ID（可选）</span>
            <Input value={threadId} onChange={setThreadId} />
          </label>
        </div>
        <div className="context-form">
          <div className="form-field">
            <span id="route-project-label">群任务关联项目（可选）</span>
            <Select
              aria-labelledby="route-project-label"
              value={projectId}
              optionList={[
                { value: "", label: "不关联项目" },
                ...(projects.data?.items ?? [])
                  .filter((item) => item.status === "active")
                  .map((item) => ({ value: item.id, label: item.name })),
              ]}
              onChange={(value) => {
                setProjectId(String(value));
                setRepositoryIds([]);
                setShareProject(false);
              }}
            />
          </div>
          {projectId && (
            <div className="form-field">
              <span id="route-repositories-label">群任务可用仓库（多选）</span>
              <Select
                aria-labelledby="route-repositories-label"
                multiple
                value={repositoryIds}
                disabled={project.isPending || project.isError}
                optionList={repositories.map((item) => ({
                  value: item.id,
                  label: item.name,
                }))}
                onChange={(value) => {
                  setRepositoryIds(value as string[]);
                  setShareProject(false);
                }}
              />
            </div>
          )}
        </div>
        {projects.isError || project.isError ? (
          <Banner
            closeIcon={null}
            type="warning"
            description="项目或仓库加载失败，请重新选择；不会自动改用默认测试仓库。"
          />
        ) : null}
        {projectId && (
          <Checkbox
            checked={shareProject}
            onChange={(event) => setShareProject(event.target.checked ?? false)}
          >
            确认所选项目资料、代码相关上下文与任务结果可分享给该群；写入、推送和
            MR 仍需逐次审批
          </Checkbox>
        )}
        <Typography.Text type="tertiary">
          保存会替换该群或话题的路由，并撤销旧项目路由任务的分享授权；不迁移旧任务到新仓库。
        </Typography.Text>
        <Checkbox
          checked={nearby}
          onChange={(event) => setNearby(event.target.checked ?? false)}
        >
          允许自动补齐同群近期邻近消息（仍受时间、权限和预算限制）
        </Checkbox>
        <Button
          htmlType="submit"
          loading={mutation.isPending}
          disabled={!sourceId || !agentId || !version || !validSelection}
        >
          保存明确路由
        </Button>
      </form>
      {mutation.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(mutation.error)}
          type="danger"
        />
      ) : null}
      <ul className="context-records">
        {routes.data?.items.map((item) => (
          <li key={item.id ?? `${item.source_id}:${item.thread_id ?? "-"}`}>
            <span>
              {item.source_id} → {item.agent_id} / {item.agent_version}
              <small>
                {item.thread_id ? `限定话题 ${item.thread_id}` : "群级路由"} ·
                邻近补齐{item.include_nearby ? "已允许" : "未启用"}
                {item.project_id
                  ? ` · 项目 ${item.project_id} · ${item.repository_ids?.length ?? 0} 个授权仓库`
                  : " · 无项目"}
              </small>
            </span>
          </li>
        ))}
      </ul>
      <Typography.Title heading={5}>入站 / 自动补齐</Typography.Title>
      <Table
        rowKey="id"
        columns={columns}
        dataSource={status.isError ? [] : (status.data?.inbox ?? [])}
        pagination={false}
      />
      <Typography.Title heading={5}>原会话出站</Typography.Title>
      <Table
        rowKey="id"
        columns={columns}
        dataSource={status.isError ? [] : (status.data?.deliveries ?? [])}
        pagination={false}
      />
    </Card>
  );
}

function channelLabel(value: string) {
  return (
    (
      {
        PENDING: "已持久接收 / 待处理",
        RECEIVED: "已持久接收 / 待处理",
        REJECTED: "身份或授权不足，已拒绝",
        PROCESSING: "处理中",
        SUBMITTED: "已创建运行",
        WAITING_CONTEXT: "等待上下文",
        WAITING_IDENTITY: "等待成员身份关联",
        SENT: "已送达原会话",
        SENDING: "发送中",
        FAILED: "投递失败",
        RECOVERY_REQUIRED: "结果未知，需核对",
        EXPIRED: "正文已过期",
        IGNORED: "已忽略",
      } as Record<string, string>
    )[value] ?? value
  );
}
