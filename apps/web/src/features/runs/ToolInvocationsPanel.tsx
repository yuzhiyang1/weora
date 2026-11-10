import { Banner, Tag, Typography } from "../../components/console";
import { useQuery } from "@tanstack/react-query";

import { consoleRequest, errorMessage } from "./api";

type ToolInvocation = {
  call_id: string;
  name: string;
  status: string;
  arguments: unknown;
  result: unknown;
  error_code: string | null;
  created_at: string;
};

/** 调用成功只表示拿到工具回执；命令的非零退出码仍是失败证据。 */
function executionEvidence(result: unknown) {
  if (!result || typeof result !== "object") return null;
  const record = result as Record<string, unknown>;
  if (record.kind !== "command") return null;
  if (record.status === "PASSED" && record.exit_code === 0)
    return { label: "命令通过 · 退出码 0", color: "green" };
  if (record.status === "FAILED" && typeof record.exit_code === "number")
    return { label: `命令未通过 · 退出码 ${record.exit_code}`, color: "red" };
  return { label: "命令结果未确认", color: "grey" };
}

export function ToolInvocationsPanel({
  workspaceId,
  userId,
  runId,
}: {
  workspaceId: string;
  userId: string;
  runId: string;
}) {
  const query = useQuery({
    queryKey: ["run-tools", workspaceId, userId, runId],
    queryFn: () =>
      consoleRequest<{ items: ToolInvocation[] }>(
        workspaceId,
        `/runs/${encodeURIComponent(runId)}/tools`,
      ),
    retry: false,
  });
  return (
    <section className="detail-card">
      <Typography.Title heading={3}>工具调用事实</Typography.Title>
      <p>
        调用请求、真实工具结果与上方 Agent
        解释分别展示；调用成功不等于命令或测试通过，未知结果不得当作失败重试。
      </p>
      {query.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(query.error)}
          type="warning"
        />
      ) : null}
      {!query.isError
        ? query.data?.items.map((item) => {
            const evidence = executionEvidence(item.result);
            return (
              <article className="run-tool" key={item.call_id}>
                <div className="run-actions">
                  <strong>{item.name}</strong>
                  <Tag
                    color={
                      item.status === "SUCCEEDED"
                        ? "green"
                        : item.status === "FAILED"
                          ? "red"
                          : "grey"
                    }
                  >
                    {(
                      {
                        SUCCEEDED: "调用成功",
                        FAILED: "调用失败",
                        RUNNING: "调用中",
                        UNKNOWN: "结果未知，需核对",
                      } as Record<string, string>
                    )[item.status] ?? item.status}
                  </Tag>
                  {item.status === "SUCCEEDED" && evidence && (
                    <Tag color={evidence.color}>{evidence.label}</Tag>
                  )}
                  <small>调用 ID：{item.call_id}</small>
                </div>
                <details>
                  <summary>平台已校验的调用参数</summary>
                  <pre className="run-text">
                    {JSON.stringify(item.arguments, null, 2)}
                  </pre>
                </details>
                <details>
                  <summary>工具事实结果</summary>
                  {item.result === null ? (
                    <p>尚无已确认结果。</p>
                  ) : (
                    <pre className="run-text">
                      {typeof item.result === "string"
                        ? item.result
                        : JSON.stringify(item.result, null, 2)}
                    </pre>
                  )}
                </details>
                {item.error_code ? <p>错误代码：{item.error_code}</p> : null}
              </article>
            );
          })
        : null}
      {!query.isError && query.data?.items.length === 0 ? (
        <p>本次运行尚无工具调用。</p>
      ) : null}
    </section>
  );
}
