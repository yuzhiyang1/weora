import {
  Banner,
  Button,
  Input,
  Select,
  Tag,
  Typography,
} from "../../components/console";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { ConsoleApiError, errorMessage } from "../runs/api";
import { formatDate } from "../skills/SkillsPage";
import {
  contextList,
  listSnapshots,
  previewSnapshot,
  readContext,
  type ContextMessage,
  type ContextReadInput,
  type ContextResource,
  type Snapshot,
  type Source,
} from "./api";
import "./context.css";

function localDate(date: Date) {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}

function contextNotConfigured(error: unknown): boolean {
  return (
    error instanceof ConsoleApiError && error.code === "CONTEXT_NOT_CONFIGURED"
  );
}

export function RunContextPanel({
  workspaceId,
  userId,
  runId,
  cutoff,
}: {
  workspaceId: string;
  userId: string;
  runId: string;
  cutoff: string;
}) {
  const client = useQueryClient();
  const key = ["run-context", workspaceId, userId, runId];
  const sources = useQuery({
    queryKey: ["context-sources", workspaceId, userId, runId],
    queryFn: () => contextList<Source>(workspaceId, "sources"),
    retry: false,
    refetchOnWindowFocus: (query) => !contextNotConfigured(query.state.error),
  });
  const snapshots = useQuery({
    queryKey: key,
    queryFn: () => listSnapshots(workspaceId, runId),
    retry: false,
    // 部署未启用飞书时不是暂时网络故障，停止轮询；配置好后由用户显式刷新。
    refetchInterval: (query) =>
      contextNotConfigured(query.state.error) ? false : 10000,
    refetchOnWindowFocus: (query) => !contextNotConfigured(query.state.error),
  });
  const [sourceId, setSourceId] = useState("");
  const [start, setStart] = useState(() =>
    localDate(new Date(new Date(cutoff).getTime() - 7 * 86400000)),
  );
  const [end, setEnd] = useState(() => localDate(new Date(cutoff)));
  const [threadId, setThreadId] = useState("");
  const [messageId, setMessageId] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [lastInput, setLastInput] = useState<ContextReadInput | null>(null);
  const selected = useQuery({
    queryKey: [...key, "preview", selectedId],
    queryFn: () => previewSnapshot(workspaceId, runId, selectedId!),
    enabled: !!selectedId,
    staleTime: 0,
    retry: false,
    refetchInterval: 5000,
  });
  const read = useMutation({
    mutationFn: (input: ContextReadInput) =>
      readContext(workspaceId, runId, input),
    onSuccess: (snapshot) => {
      setSelectedId(snapshot.snapshot_id);
      void client.invalidateQueries({ queryKey: key });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const input: ContextReadInput = {
      source_id: sourceId,
      start_time: new Date(start).toISOString(),
      end_time: new Date(end).toISOString(),
      limit: 30,
      ...(threadId.trim() ? { thread_id: threadId.trim() } : {}),
      ...(messageId.trim() ? { message_id: messageId.trim() } : {}),
    };
    setLastInput(input);
    read.mutate(input);
  }
  // 列表缓存不承载预览正文；每次点开和持续展示都重新核验来源及 Run 权限。
  const snapshot = selected.isError ? undefined : selected.data;
  const continuation: ContextReadInput | null =
    snapshot?.query && snapshot.window && snapshot.source_id
      ? {
          source_id: snapshot.source_id,
          start_time: snapshot.window.start_time,
          end_time: snapshot.window.end_time,
          limit: snapshot.query.limit,
          ...(snapshot.query.thread_id
            ? { thread_id: snapshot.query.thread_id }
            : {}),
          ...(snapshot.query.message_id
            ? { message_id: snapshot.query.message_id }
            : {}),
        }
      : lastInput && snapshot?.source_id === lastInput.source_id
        ? lastInput
        : null;
  if (
    contextNotConfigured(sources.error) ||
    contextNotConfigured(snapshots.error)
  ) {
    return (
      <section className="detail-card">
        <div className="run-section-heading">
          <Typography.Title heading={3}>飞书上下文与引用</Typography.Title>
          <Button
            onClick={() => {
              void snapshots.refetch();
              void sources.refetch();
            }}
          >
            刷新授权来源
          </Button>
        </div>
        <Banner
          closeIcon={null}
          description="飞书上下文尚未启用，不影响当前文本任务。"
          type="info"
        />
      </section>
    );
  }
  return (
    <section className="detail-card">
      <div className="run-section-heading">
        <Typography.Title heading={3}>飞书上下文与引用</Typography.Title>
        <Button
          onClick={() => {
            void snapshots.refetch();
            void sources.refetch();
          }}
        >
          刷新授权来源
        </Button>
      </div>
      <p>
        手动读取与 Agent 工具共用同一 Run 预算。时间以本地时区选择、转换为 UTC
        发送；默认运行截止点前 7 天，最多 30 天，不读取触发任务之后的新消息。
      </p>
      {sources.isError || snapshots.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(sources.error ?? snapshots.error)}
          type="warning"
        />
      ) : null}
      <form className="agent-form" onSubmit={submit}>
        <div className="form-field">
          <span id="run-context-source-label">授权群来源</span>
          <Select
            aria-labelledby="run-context-source-label"
            value={sourceId || undefined}
            placeholder="仅显示当前身份有权读取的来源"
            optionList={(sources.data?.items ?? []).map((source) => ({
              value: source.id,
              label: source.name,
            }))}
            onChange={(value) => setSourceId(String(value))}
          />
        </div>
        <div className="context-window">
          <label className="form-field">
            <span>起始时间</span>
            <Input
              aria-label="起始时间"
              required
              type="datetime-local"
              value={start}
              onChange={setStart}
            />
          </label>
          <label className="form-field">
            <span>截止时间</span>
            <Input
              aria-label="截止时间"
              required
              type="datetime-local"
              value={end}
              min={start}
              max={localDate(new Date(cutoff))}
              onChange={setEnd}
            />
          </label>
        </div>
        <div className="context-form">
          <label className="form-field">
            <span>话题 ID（可选）</span>
            <Input value={threadId} onChange={setThreadId} />
          </label>
          <label className="form-field">
            <span>消息 ID（可选）</span>
            <Input value={messageId} onChange={setMessageId} />
          </label>
          <Button
            htmlType="submit"
            loading={read.isPending}
            disabled={
              !sourceId ||
              !start ||
              !end ||
              end <= start ||
              (!!threadId.trim() && !!messageId.trim())
            }
          >
            读取并保存快照
          </Button>
        </div>
        {threadId.trim() && messageId.trim() ? (
          <p role="alert">话题与单消息查询只能选择一种，请清空其中一个 ID。</p>
        ) : null}
      </form>
      {read.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(read.error)}
          type="danger"
        />
      ) : null}
      <div className="run-actions context-snapshot-list">
        {snapshots.data?.items.map((item) => (
          <Button
            key={item.snapshot_id}
            disabled={item.expired}
            onClick={() => {
              setSelectedId(item.snapshot_id);
              void client.invalidateQueries({
                queryKey: [...key, "preview", item.snapshot_id],
              });
            }}
          >
            {item.expired
              ? "快照已过期"
              : `预览 ${item.snapshot_id.slice(0, 8)} · ${coverageLabel(item)}`}
          </Button>
        ))}
      </div>
      {selected.isError ? (
        <Banner
          closeIcon={null}
          title="快照不可预览"
          description={errorMessage(selected.error)}
          type="warning"
        />
      ) : null}
      {snapshot ? (
        <>
          <SnapshotPreview snapshot={snapshot} />
          {snapshot.next_cursor && continuation ? (
            <Button
              loading={read.isPending}
              onClick={() =>
                read.mutate({ ...continuation, cursor: snapshot.next_cursor! })
              }
            >
              按同一窗口读取下一页
            </Button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

export function coverageLabel(snapshot: Snapshot) {
  return snapshot.status === "complete"
    ? "本次窗口遍历结束"
    : snapshot.status === "partial"
      ? "部分覆盖"
      : "不可用";
}

function contentTypeLabel(type: string) {
  const labels: Record<string, string> = {
    text: "纯文本",
    post: "富文本",
    system: "系统消息",
    interactive: "消息卡片",
    image: "图片",
    file: "文件",
    folder: "文件夹",
    audio: "语音",
    media: "视频",
    sticker: "表情",
    deleted: "已撤回消息",
  };
  // 消息类型来自外部系统；constructor/__proto__不能读取对象继承属性。
  return Object.hasOwn(labels, type) ? labels[type] : type;
}

function parseStatusLabel(status: ContextMessage["parse_status"]) {
  switch (status) {
    case "complete":
      return "正文已解析";
    case "partial":
      return "内容部分解析";
    case "unsupported":
      return "正文暂不支持解析";
    case "recalled":
      return "消息已撤回";
    case undefined:
      return "旧快照：未记录解析状态";
    default:
      return "解析状态未识别";
  }
}

function parseNoteLabel(note: string) {
  const labels: Record<string, string> = {
    unknown_post_node: "富文本包含尚未识别的节点",
    resource_not_downloaded: "只保留资源信息，未下载或解析资源正文",
    malformed_content: "消息内容结构无法完整解析",
    content_truncated: "消息内容超过解析上限",
    unknown_card_node: "卡片包含尚未识别的节点",
    unknown_system_variable: "系统消息包含尚未识别的变量",
    card_interaction_not_executed: "仅显示按钮文字，未执行卡片交互",
    unsupported_message_type: "此消息类型暂未实现正文解析",
    message_recalled: "不恢复已撤回的正文或资源",
  };
  return Object.hasOwn(labels, note) ? labels[note] : `解析说明：${note}`;
}

function ResourceMetadata({ resource }: { resource: ContextResource }) {
  return (
    <li>
      <span>
        {contentTypeLabel(resource.type)}
        {resource.file_name ? `：${resource.file_name}` : ""}
      </span>
      <small>仅资源信息 · 未下载 · 未解析内容</small>
      {/* 标识不是下载URL；不能用它拼接链接或自动请求外部图片。 */}
      {resource.file_key ? <code>文件标识：{resource.file_key}</code> : null}
      {resource.image_key ? <code>图片标识：{resource.image_key}</code> : null}
    </li>
  );
}

export function SnapshotPreview({ snapshot }: { snapshot: Snapshot }) {
  return (
    <section aria-label="上下文快照预览" className="context-preview">
      <div className="run-actions">
        <Tag color={snapshot.status === "complete" ? "green" : "orange"}>
          {coverageLabel(snapshot)}
        </Tag>
        <small>快照 {snapshot.snapshot_id}</small>
      </div>
      {snapshot.window ? (
        <p>
          {formatDate(snapshot.window.start_time)} —{" "}
          {formatDate(snapshot.window.end_time)}
        </p>
      ) : null}
      {snapshot.coverage?.reason ? (
        <Banner
          closeIcon={null}
          title="覆盖范围有缺口"
          description={`原因：${snapshot.coverage.reason}。不能据此断言其他历史没有相关方案。`}
          type="warning"
        />
      ) : null}
      {snapshot.error ? (
        <Banner
          closeIcon={null}
          description={`${snapshot.error.code}：${snapshot.error.message}`}
          type="warning"
        />
      ) : null}
      {snapshot.messages?.length === 0 ? (
        <p>此页没有可交付的消息；请结合覆盖范围判断。</p>
      ) : null}
      {snapshot.messages?.map((message) => (
        <article className="context-message" key={message.ref}>
          <header>
            <code>{message.ref}</code>
            <span>外部作者：{message.author_id}</span>
            <time>{formatDate(message.created_at)}</time>
          </header>
          <div className="run-actions context-message-status">
            <Tag>{contentTypeLabel(message.content_type)}</Tag>
            <span>{parseStatusLabel(message.parse_status)}</span>
          </div>
          <p>{message.text}</p>
          {message.parse_notes?.length ? (
            <ul className="context-parse-notes">
              {message.parse_notes.map((note, index) => (
                <li key={`${note}:${index}`}>{parseNoteLabel(note)}</li>
              ))}
            </ul>
          ) : null}
          {message.parse_status !== "recalled" && message.resources?.length ? (
            <ul aria-label="消息资源信息" className="context-resources">
              {message.resources.map((resource, index) => (
                <ResourceMetadata key={index} resource={resource} />
              ))}
            </ul>
          ) : null}
          {message.thread_id ? <small>话题：{message.thread_id}</small> : null}
        </article>
      ))}
      {snapshot.expires_at ? (
        <small>
          正文保留至 {formatDate(snapshot.expires_at)}，撤权后将立即停止预览。
        </small>
      ) : null}
    </section>
  );
}
