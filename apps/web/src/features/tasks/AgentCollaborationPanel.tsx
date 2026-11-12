import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Banner, Button, Card, Modal, Spin, Tag } from "../../components/console";
import { errorMessage } from "../runs/api";
import { getTaskAgentMail, type AgentSession } from "./agent-collaboration-api";
import "./agent-collaboration.css";

const receiptLabels: Record<string, string> = {
  pending: "待领取",
  claimed: "已领取",
  needs_confirmation: "结果待确认",
  replied: "已回复",
  acknowledged: "已确认",
  expired: "已过期",
  cancelled: "已停止",
};

export function SessionStatus({ session }: { session: AgentSession }) {
  if (!session.online) return <Tag color="grey">设备离线</Tag>;
  return session.delivery === "manual"
    ? <Tag color="blue">在线 · 主动领取</Tag>
    : <Tag color="green">在线 · 支持自动唤醒</Tag>;
}

export function CopyInstruction({ text, label }: { text: string; label: string }) {
  const [status, setStatus] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setStatus("已复制");
    } catch {
      setStatus("请选中文字手动复制");
    }
  }
  return (
    <div className="agent-collab-stack">
      <code className="agent-collab-command">{text}</code>
      <div className="agent-collab-actions">
        <Button size="small" onClick={() => void copy()}>{label}</Button>
        <span role="status" className="task-muted">{status}</span>
      </div>
    </div>
  );
}

export function AgentCollaborationPanel({
  workspaceId, taskId, memberId, canJoin,
}: { workspaceId: string; taskId: string; memberId: string; canJoin: boolean }) {
  const [join, setJoin] = useState(false);
  const [target, setTarget] = useState<AgentSession>();
  const data = useQuery({
    queryKey: ["task-agent-collaboration", workspaceId, taskId],
    queryFn: () => getTaskAgentMail(workspaceId, taskId),
    refetchInterval: 8000,
    retry: false,
  });
  const names = new Map(data.data?.sessions.map((session) => [session.id, session.name]));
  return (
    <Card title="本地 Agent 协作" headerExtraContent={canJoin && (
      <Button size="small" onClick={() => setJoin(true)}>接入本地会话</Button>
    )}>
      <div className="agent-collab-stack">
        {data.isPending && <Spin tip="正在查看可对话会话" />}
        {data.isError && <Banner type="danger" description={errorMessage(data.error)} />}
        {data.data && (
          <>
            <section className="agent-collab-stack">
              <h3>可对话会话</h3>
              {!data.data.sessions.length && <p className="task-muted">暂无已接入会话。</p>}
              {data.data.sessions.map((session) => (
                <div className="agent-collab-row" key={session.id}>
                  <div>
                    <strong>{session.name}</strong>
                    <p className="task-muted">{[session.owner_name, session.runtime, session.repo, session.branch]
                      .filter(Boolean).join(" · ")}</p>
                  </div>
                  <div className="agent-collab-actions">
                    <SessionStatus session={session} />
                    {canJoin && session.owner_id !== memberId && (
                      <Button size="small" onClick={() => setTarget(session)}>发问指令</Button>
                    )}
                  </div>
                </div>
              ))}
            </section>
            <section className="agent-collab-stack">
              <h3>联调消息</h3>
              {!data.data.messages.length && <p className="task-muted">暂无消息。</p>}
              {data.data.messages.map((message) => (
                <article key={message.id} className="agent-collab-message">
                  <div className="agent-collab-actions">
                    <strong>{names.get(message.sender_id) ?? "已离开的会话"} → {names.get(message.recipient_id) ?? "已离开的会话"}</strong>
                    <Tag>{receiptLabels[message.state] ?? message.state}</Tag>
                    {message.state === "pending" && message.wake_state === "accepted" && <Tag>唤醒提示已入队</Tag>}
                    {message.state === "pending" && message.wake_state === "unknown" && <Tag>唤醒结果待确认</Tag>}
                  </div>
                  <p>{message.body}</p>
                  <time className="task-muted" dateTime={message.created_at}>{new Date(message.created_at).toLocaleString("zh-CN")}</time>
                </article>
              ))}
            </section>
            <section className="agent-collab-stack">
              <h3>共享证据</h3>
              {!data.data.evidence.length && <p className="task-muted">暂无已发布证据。</p>}
              {data.data.evidence.map((item) => (
                <article className="agent-collab-message" key={item.id}>
                  <strong>{item.title}</strong>
                  <p>{item.content}</p>
                </article>
              ))}
            </section>
          </>
        )}
      </div>
      {join && (
        <Modal title="接入本地会话" visible onCancel={() => setJoin(false)}>
          <div className="agent-collab-stack">
            <p>在要共享的原生会话里粘贴指令。任务参与者可以联系这条会话，原有历史仍保留在本机。</p>
            <CopyInstruction label="复制加入指令" text={`用 Weora 将当前会话接入协作任务 ${taskId}。`} />
            <Link className="task-link" to="/settings/local-agents">设置本地 Agent</Link>
          </div>
        </Modal>
      )}
      {target && (
        <Modal title="让我的 Agent 发问" visible onCancel={() => setTarget(undefined)}>
          <div className="agent-collab-stack">
            <p>把指令贴进已接入此任务的本地会话，再写上你想问的问题。</p>
            <CopyInstruction
              label="复制对话指令"
              text={`用 Weora 在协作任务 ${taskId} 联系会话 ${target.id}。我想问：`}
            />
          </div>
        </Modal>
      )}
    </Card>
  );
}
