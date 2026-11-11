import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Bot, MessageCircle, User } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Banner, Button, Card, Select, Spin, Tag, TextArea } from "../../components/console";
import { MarkdownContent } from "../../components/MarkdownContent";
import { formatDate } from "../skills/SkillsPage";
import {
  ConsoleApiError,
  errorMessage,
  getRunConversation,
  isTerminal,
  runKey,
  sendRunInput,
  type RunConversation,
  type RunDetail,
  type RunInputMode,
} from "./api";
import { RunStatusTag } from "./RunStatusTag";

const receipts = {
  PENDING: { text: "已排队 · 等待后续运行", color: "orange" },
  DISPATCHED: { text: "已创建后续运行", color: "blue" },
  REJECTED: { text: "未进入后续运行", color: "grey" },
  STEERING: { text: "等待模型读取 · 本轮运行", color: "blue" },
  READ: { text: "模型已读 · 本轮运行", color: "green" },
  MISSED: { text: "本轮未读取 · 可重新发送", color: "orange" },
} as const;

function conversationEntries(conversation: RunConversation) {
  const nextRuns = new Set(conversation.inputs.filter((input) => input.mode === "continue").map((input) => input.run_id));
  const turns = conversation.runs.flatMap((turn) => [
    ...(nextRuns.has(turn.id) ? [] : [{
      id: `request:${turn.id}`, text: turn.instruction, date: turn.created_at, author: null, input: null,
    }]),
    ...(turn.output ? [{
      id: `answer:${turn.id}`, text: turn.output, date: turn.updated_at, author: turn.agent_name, input: null,
    }] : []),
  ]);
  return [
    ...turns,
    ...conversation.inputs.map((input) => ({
      id: input.id, text: input.instruction, date: input.created_at, author: null, input,
    })),
  ].sort((left, right) => Date.parse(left.date) - Date.parse(right.date));
}

export function RunConversationPanel({ workspaceId, userId, run, onAccessRevoked, onShowTools }: {
  workspaceId: string;
  userId: string;
  run: RunDetail;
  onAccessRevoked: () => void;
  onShowTools: () => void;
}) {
  const client = useQueryClient();
  const key = ["run-conversation", workspaceId, userId, run.id];
  const editorId = useId();
  const editor = useRef<HTMLTextAreaElement>(null);
  const thread = useRef<HTMLOListElement>(null);
  const [draft, setDraft] = useState("");
  const [selection, setSelection] = useState<RunInputMode | null>(null);
  const pendingRequest = useRef<{ text: string; mode: RunInputMode; id: string } | null>(null);
  const conversation = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => getRunConversation(workspaceId, run.id, signal),
    refetchInterval: (query) => query.state.data && (
      !isTerminal(run.status) ||
      query.state.data.runs.some((item) => !isTerminal(item.status)) ||
      query.state.data.inputs.some((item) => ["STEERING", "PENDING"].includes(item.status))
    ) ? 3000 : false,
    retry: false,
  });
  const data = conversation.data;
  const hasConversation = Boolean(data);
  const mode = selection ?? (data?.can_steer ? "steer" : "continue");
  const submit = useMutation({
    mutationFn: ({ text, mode, id }: { text: string; mode: RunInputMode; id: string }) =>
      sendRunInput(workspaceId, run.id, text, mode, id),
    onSuccess: async () => {
      pendingRequest.current = null;
      setDraft("");
      setSelection(null);
      await Promise.all([
        client.invalidateQueries({ queryKey: key }),
        client.invalidateQueries({ queryKey: runKey(workspaceId, userId), exact: true }),
      ]);
      if (thread.current) thread.current.scrollTop = thread.current.scrollHeight;
      editor.current?.focus();
    },
    onError: () => void conversation.refetch(),
  });

  useEffect(() => {
    if (conversation.error instanceof ConsoleApiError && [401, 403, 404].includes(conversation.error.status)) {
      onAccessRevoked();
    }
  }, [conversation.error, onAccessRevoked]);
  useEffect(() => {
    if (thread.current) thread.current.scrollTop = thread.current.scrollHeight;
  }, [hasConversation]);

  if (conversation.isError) return <Banner type="danger" title="无法读取对话" description={
    <Button onClick={() => void conversation.refetch()}>重试</Button>
  } />;
  if (!data) return <Spin tip="正在读取 Agent 对话" />;

  const turns = conversationEntries(data);
  const invalidMode = mode === "steer" && !data.can_steer;
  const taskGoal = data.runs[0]?.instruction ?? run.instruction;
  return (
    <div className="run-conversation-grid">
      <Card className="run-conversation" title={
        <span className="run-panel-title"><MessageCircle size={18} aria-hidden="true" />Agent 对话</span>
      }>
        <ol className="run-conversation-thread" aria-label="对话消息" ref={thread} tabIndex={0}>
          {turns.map((turn) => (
            <li className="run-conversation-message" key={turn.id}>
              <span className="run-conversation-avatar" aria-hidden="true">
                {turn.author ? <Bot size={16} /> : <User size={16} />}
              </span>
              <div className="run-conversation-entry">
                <div className="run-conversation-meta">
                  <strong>{turn.author ?? "你"}</strong><time dateTime={turn.date}>{formatDate(turn.date)}</time>
                </div>
                <div className="run-conversation-bubble">
                  {turn.author ? <MarkdownContent>{turn.text}</MarkdownContent> : <p>{turn.text}</p>}
                </div>
                {turn.input && (
                  <div className="run-conversation-receipt">
                    <Tag color={receipts[turn.input.status].color}>{receipts[turn.input.status].text}</Tag>
                    {turn.input.mode === "continue" && turn.input.run_id && turn.input.run_id !== run.id && (
                      <Link to={`/runs/${turn.input.run_id}`}>查看后续运行</Link>
                    )}
                    {turn.input.status === "MISSED" && data.can_send && !draft && (
                      <Button size="small" theme="borderless" onClick={() => {
                        setDraft(turn.text);
                        setSelection("continue");
                        editor.current?.focus();
                      }}>填回输入框</Button>
                    )}
                  </div>
                )}
              </div>
            </li>
          ))}
        </ol>
        {data.can_send && (
          <form className="run-conversation-composer" onSubmit={(event) => {
            event.preventDefault();
            const text = draft.trim();
            if (!text || invalidMode || submit.isPending) return;
            // 请求超时可用同一提交键重试，避免重复指令。
            if (pendingRequest.current?.text !== text || pendingRequest.current.mode !== mode) {
              pendingRequest.current = { text, mode, id: crypto.randomUUID() };
            }
            submit.mutate(pendingRequest.current);
          }}>
            <label htmlFor={editorId}>补充指令</label>
            <TextArea id={editorId} ref={editor} value={draft} placeholder="补充要求或追问…"
              rows={2} maxLength={16000} disabled={submit.isPending} onChange={(value) => {
                if (!draft) setSelection(mode);
                setDraft(value);
              }} />
            <div className="run-conversation-composer-actions">
              <Select aria-label="发送方式" value={mode} disabled={submit.isPending}
                optionList={[
                  { label: "加入当前运行", value: "steer", disabled: !data.can_steer },
                  { label: isTerminal(run.status) ? "开始后续运行" : "本轮结束后继续", value: "continue" },
                ]}
                onChange={(value) => { if (value === "steer" || value === "continue") setSelection(value); }} />
              <Button type="primary" theme="solid" htmlType="submit" icon={<ArrowUp size={16} />} loading={submit.isPending}
                disabled={!draft.trim() || invalidMode}>发送</Button>
            </div>
            {invalidMode && <p role="status">本轮已无法接收消息，请选择后续运行。</p>}
            {submit.isError && <p role="alert">{errorMessage(submit.error)}</p>}
          </form>
        )}
      </Card>
      <aside className="run-conversation-sidebar" aria-label="运行信息">
        <Card title="本轮运行" headerExtraContent={<RunStatusTag status={run.status} />}>
          <dl className="run-metadata">
            <dt>Agent</dt><dd>{run.agent_name}</dd>
            <dt>版本</dt><dd>{run.agent_version}</dd>
            <dt>创建时间</dt><dd>{formatDate(run.created_at)}</dd>
          </dl>
          <Button size="small" theme="borderless" onClick={onShowTools}>查看工具调用</Button>
        </Card>
        <Card title="任务目标">
          <p className="run-conversation-goal">{taskGoal}</p>
        </Card>
      </aside>
    </div>
  );
}
