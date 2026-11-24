import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Banner, Button, Card, Modal, Select } from "../../components/console";
import type { Scope } from "../knowledge/api";
import { getProject, listProjects, projectKey } from "../projects/api";
import { errorMessage } from "../runs/api";
import { writeWorkflowScoreSync, type WorkflowScoreSyncStatus } from "./api";
import { timestamp } from "./report-controls";

const statuses: Record<WorkflowScoreSyncStatus["status"], string> = {
  unconfigured: "未配置", unavailable: "同步服务未启用", disabled: "已停用",
  queued: "等待同步", syncing: "同步中", failed: "同步失败，等待重试",
  ready: "已同步", source_changed: "仓库连接已变化",
};
const errors: Record<string, string> = {
  WORKFLOW_SCORE_SYNC_BRANCH_MISSING: "仓库尚未发布 teamai-reports 分支。",
  WORKFLOW_SCORE_SYNC_TIMEOUT: "读取报表分支超时。",
  REPOSITORY_TIMEOUT: "读取报表分支超时。",
  KNOWLEDGE_WORKFLOW_SCORE_FORMAT: "报表分支中存在格式无效的评分记录。",
  WORKFLOW_SCORE_SYNC_MONTH: "有评分记录与其文件月份不一致。",
  WORKFLOW_SCORE_SYNC_LIMIT: "报表体量超过单次同步预算。",
};

export function WorkflowScoreSyncPanel({ scope, user, status, canConfigure, pending, failed, retry, updated }: {
  scope: Scope; user: string; status?: WorkflowScoreSyncStatus | undefined; canConfigure: boolean;
  pending: boolean; failed: boolean; retry: () => void; updated: (status: WorkflowScoreSyncStatus) => void;
}) {
  const [open, setOpen] = useState(false);
  const state = pending ? "正在读取同步状态" : failed ? "无法读取同步状态"
    : !status?.available && status?.status === "unconfigured" ? "同步服务未启用"
      : status ? statuses[status.status] : "未配置";
  const detail = status && [
    status.repository_name, status.branch, status.last_synced_at ? "上次同步 " + timestamp(status.last_synced_at) : "",
  ].filter(Boolean).join(" · ");
  const error = status?.status === "failed" && (errors[status.error_code ?? ""] ?? "读取报表分支失败。");
  return <>
    <Card title={"团队报表自动同步 · " + state}>
      <div className="report-inline sync-status-line">
        <div className="report-muted" aria-live="polite">
          {detail && <p>{detail}</p>}
          {error && <p>{error}{status?.last_synced_at && " 当前使用上次同步结果。"}</p>}
          {status?.status === "source_changed" && <p>请检查当前仓库连接并重新保存设置。</p>}
        </div>
        {failed && <Button onClick={retry}>重试状态查询</Button>}
        {canConfigure && status && (status.available || status.version > 0) &&
          <Button onClick={() => setOpen(true)}>自动同步设置</Button>}
      </div>
    </Card>
    {open && status && <SyncSettingsDialog scope={scope} status={status} user={user} close={() => setOpen(false)}
      saved={(result) => { updated(result); setOpen(false); }} />}
  </>;
}

function SyncSettingsDialog({ scope, status, user, close, saved }: {
  scope: Scope; status: WorkflowScoreSyncStatus; user: string; close: () => void;
  saved: (status: WorkflowScoreSyncStatus) => void;
}) {
  const [project, setProject] = useState(scope.project || status.source_project_id || "");
  const [repository, setRepository] = useState(status.repository_id ?? "");
  const [expectedVersion] = useState(status.version);
  const projectList = useQuery({
    queryKey: projectKey(scope.workspace, user), queryFn: () => listProjects(scope.workspace), enabled: !scope.project,
  });
  const details = useQuery({
    queryKey: projectKey(scope.workspace, user, project), queryFn: () => getProject(scope.workspace, project),
    enabled: !!project,
  });
  const options = (details.data?.repositories ?? []).filter((item) =>
    item.status === "active" && item.connection_status === "connected",
  );
  const hasSource = options.some((item) => item.id === repository);
  const request = useMutation({
    mutationFn: (enabled: boolean) => writeWorkflowScoreSync(scope, {
      expected_version: expectedVersion, enabled,
      ...(enabled ? { source_project_id: project, repository_id: repository } : {}),
    }),
    onSuccess: saved,
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (hasSource && status.available && !request.isPending) request.mutate(true);
  }
  const error = request.error ?? details.error ?? projectList.error;
  return <Modal visible title="自动同步设置" onCancel={close}
    maskClosable={!request.isPending} closeOnEsc={!request.isPending}>
    <form className="report-stack" onSubmit={submit}>
      <p>将已连接团队仓库的 <code>{status.branch}</code> 分支同步到{scope.project ? "当前项目" : "工作空间共享范围"}。</p>
      {!scope.project && <div className="sync-source-field">
        <span id="workflow-score-sync-project">来源项目</span>
        <Select aria-labelledby="workflow-score-sync-project" value={project || undefined}
          loading={projectList.isPending} disabled={request.isPending}
          onChange={(value) => { setProject(String(value)); setRepository(""); }}
          optionList={(projectList.data?.items ?? []).filter((item) => item.status === "active")
            .map(({ id, name }) => ({ value: id, label: name }))} />
      </div>}
      <div className="sync-source-field">
        <span id="workflow-score-sync-repository">团队仓库</span>
        <Select aria-labelledby="workflow-score-sync-repository" value={hasSource ? repository : undefined}
          loading={!!project && details.isPending} disabled={!options.length || request.isPending}
          onChange={(value) => setRepository(String(value))}
          optionList={options.map(({ id, name }) => ({ value: id, label: name }))} />
        {project && details.isSuccess && options.length === 0 && <p className="report-muted">此项目暂无已通过连接检查的仓库。</p>}
      </div>
      {error && <Banner type="danger" description={errorMessage(error)} />}
      <div className="modal-actions">
        {status.version > 0 && status.status !== "disabled" && <Button disabled={request.isPending}
          onClick={() => request.mutate(false)}>停用</Button>}
        <Button disabled={request.isPending} onClick={close}>取消</Button>
        <Button htmlType="submit" theme="solid" loading={request.isPending}
          disabled={!hasSource || !status.available}>保存并启用</Button>
      </div>
    </form>
  </Modal>;
}
