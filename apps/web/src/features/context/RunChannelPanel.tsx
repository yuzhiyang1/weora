import { Banner, Tag, Typography } from "../../components/console";
import { useQuery } from "@tanstack/react-query";

import { ConsoleApiError, errorMessage } from "../runs/api";
import { getRunChannelStatus } from "./api";
import "./context.css";

const deliveryLabels: Record<string, string> = {
  PENDING: "待投递",
  SENDING: "投递中，尚未确认",
  SENT: "已送达",
  RECOVERY_REQUIRED: "结果未知，需核对",
  EXPIRED: "已过期",
  WITHDRAWN: "已撤回",
};

function deliveryTitle(key: string) {
  if (key.startsWith("run-result:") || key.startsWith("coordination:result:"))
    return "执行结果回报";
  if (key.startsWith("coordination:ack:")) return "接单确认";
  if (key.startsWith("coordination:attention:")) return "等待人工处理";
  if (key.startsWith("coordination:answer:")) return "问题回复";
  return "状态通知";
}

export function RunChannelPanel({
  workspaceId,
  userId,
  runId,
}: {
  workspaceId: string;
  userId: string;
  runId: string;
}) {
  const query = useQuery({
    // 沿用 Run 私有缓存前缀，任务撤权或切换身份时由父页面统一清除。
    queryKey: ["run-context", workspaceId, userId, runId, "channel-status"],
    queryFn: () => getRunChannelStatus(workspaceId, runId),
    retry: false,
    refetchInterval: (state) =>
      state.state.error instanceof ConsoleApiError &&
      state.state.error.code === "CONTEXT_NOT_CONFIGURED"
        ? false
        : 5000,
  });
  // 失败时不展示先前缓存的回执；执行成功从不隐含消息已送达。
  const data = query.isError ? undefined : query.data;
  return (
    <section className="detail-card">
      <Typography.Title heading={3}>飞书入口与原会话回报</Typography.Title>
      <p>
        入口、执行、投递分别记录。这里仅展示已保存的渠道事实，不以 Agent
        自述替代回执。
      </p>
      {query.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(query.error)}
          type="warning"
        />
      ) : null}
      {data?.inbox ? (
        <>
          {data.inbox.ingress === "manual_history" ? (
            <Banner
              closeIcon={null}
              description="人工补收，不代表长连接事件已收到。"
              type="warning"
            />
          ) : data.inbox.ingress === "compensation" ? (
            <Banner
              closeIcon={null}
              description="游标补偿接收，不代表实时事件已收到。"
              type="warning"
            />
          ) : (
            <p>入口方式：飞书事件</p>
          )}
          {data.inbox.addressed !== undefined ? (
            <p>
              触发方式：
              {data.inbox.addressed ? "显式 @ 机器人" : "普通消息（未 @）"}
              {data.inbox.coordination_scope_id
                ? " · 由主 Agent 接单并管理执行会话"
                : ""}
            </p>
          ) : null}
          <p className="context-secondary">
            原消息：{data.inbox.message_id} · 入站状态：
            {data.inbox.status === "COORDINATING"
              ? "主 Agent 已接收"
              : data.inbox.status}
          </p>
          <ul className="context-records">
            {data.deliveries.map((item) => (
              <li key={item.id}>
                <span>
                  <strong>
                    {deliveryTitle(item.business_key)}
                  </strong>
                  <small>尝试 {item.attempts} 次</small>
                  {item.status === "SENT" && item.remote_message_id ? (
                    <small>远端消息：{item.remote_message_id}</small>
                  ) : null}
                </span>
                <Tag
                  className="channel-status-tag"
                  color={item.status === "SENT" ? "green" : "grey"}
                >
                  {deliveryLabels[item.status] ?? item.status}
                </Tag>
              </li>
            ))}
          </ul>
          {!data.deliveries.length ? <p>尚无投递回执，不表示已送达。</p> : null}
        </>
      ) : data ? (
        <p>尚无原会话入站记录。</p>
      ) : null}
    </section>
  );
}
