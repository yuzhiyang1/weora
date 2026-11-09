import { Banner, Tag, Typography } from "../../components/console";
import { useQuery } from "@tanstack/react-query";
import type { ProjectSnapshot, RunRepository } from "./api";
import { consoleRequest, errorMessage } from "../runs/api";
import "./projects.css";

export function RunProjectPanel({
  snapshot,
  workspaceId,
  userId,
  runId,
}: {
  snapshot: ProjectSnapshot;
  workspaceId?: string;
  userId?: string;
  runId?: string;
}) {
  const prepared = useQuery({
    queryKey: ["run-repositories", workspaceId, userId, runId],
    queryFn: () =>
      consoleRequest<{ items: RunRepository[] }>(
        workspaceId ?? "",
        `/runs/${runId}/repositories`,
      ),
    enabled:
      !!workspaceId && !!runId && snapshot.read_protocol === "git-objects-v1",
    retry: false,
    refetchInterval: 3000,
  });
  return (
    <section
      className="asset-surface project-overview"
      aria-label="本次项目上下文"
    >
      <Typography.Title heading={4}>本次项目上下文</Typography.Title>
      <Typography.Paragraph>
        {snapshot.name} · 固定资料 v{snapshot.version}
      </Typography.Paragraph>
      <Banner
        closeIcon={null}
        type="info"
        description="以下为冻结资料，只包含本次选择的仓库且不含凭据。代码准备状态另列；准备成功不代表 Agent 已读文件，实际读取以工具调用证据为准。后续编辑不会改写此资料快照。"
      />
      <dl className="project-facts">
        <dt>业务说明</dt>
        <dd>{snapshot.description || "未填写"}</dd>
        <dt>整体架构</dt>
        <dd>{snapshot.architecture || "未填写"}</dd>
        <dt>研发约定</dt>
        <dd>{snapshot.conventions || "未填写"}</dd>
      </dl>
      {prepared.isError && (
        <Banner
          type="warning"
          closeIcon={null}
          description={errorMessage(prepared.error)}
        />
      )}
      {snapshot.repositories.map((repository) => (
        <article className="project-repository" key={repository.id}>
          <Typography.Title heading={5}>
            {repository.name} <Tag>资料 v{repository.version}</Tag>
          </Typography.Title>
          <p>{repository.purpose}</p>
          {snapshot.read_protocol === "git-objects-v1" &&
            (() => {
              const code = prepared.data?.items.find(
                (item) => item.repository_id === repository.id,
              );
              return (
                <div className="project-code-status">
                  <Tag>
                    {code?.status === "READY"
                      ? "只读快照就绪"
                      : code?.status === "FAILED"
                        ? "准备失败"
                        : "等待/正在准备"}
                  </Tag>{" "}
                  {code?.revision && <code>固定提交：{code.revision}</code>}{" "}
                  {code?.error_code}
                </div>
              );
            })()}
          <code>{repository.git_url}</code>
          <dl className="project-facts">
            <dt>平台 / 分支</dt>
            <dd>
              {repository.provider} / {repository.default_branch}
            </dd>
            <dt>技术栈</dt>
            <dd>{repository.technology || "未填写"}</dd>
            <dt>开发说明</dt>
            <dd>{repository.development_notes || "未填写"}</dd>
          </dl>
        </article>
      ))}
    </section>
  );
}
