import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Banner, Button, Card, Input, Skeleton, TextArea, Typography } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { useAuth } from "../auth/AuthProvider";
import { listProjects, projectKey } from "../projects/api";
import { errorMessage } from "../runs/api";
import {
  decideWorkflow, saveWorkflowArchive, saveWorkflowDrafts, startWorkflow, workflowRun,
  type KnowledgeWorkflowRun, type KnowledgeWorkflowStart, type Scope, type WorkflowApprovalDecision,
} from "./api";
import "./knowledge.css";

export function KnowledgeWorkflowPage() {
  const { principal } = useAuth();
  const { run = "" } = useParams();
  const [search] = useSearchParams();
  const workspace = search.get("workspace");
  const project = search.get("project") ?? "";
  const back = new URLSearchParams({ view: "workflows" });
  if (project && workspace === principal?.workspace.id) back.set("project", project);
  return <section className="asset-page page-enter knowledge-workbench">
    <header className="page-header knowledge-workbench-header">
      <Typography.Title heading={1}>确认知识工作流</Typography.Title>
      <Link to={`/knowledge?${back}`}>返回知识库</Link>
    </header>
    {principal && principal.workspace.id === workspace ? (
      run === "start" ? <WorkflowStart scope={{ workspace, project }} search={search} user={principal.user.id}
        workspaceName={principal.workspace.name} /> :
        <WorkflowApproval key={`${workspace}:${principal.user.id}:${project}:${run}`}
          scope={{ workspace, project }} user={principal.user.id} workspaceName={principal.workspace.name} run={run} />
    ) : <Banner closeIcon={null} type="warning" description="请切换至发起工作流时所在的工作空间。" />}
  </section>;
}

function useDestination(scope: Scope, user: string) {
  const projects = useQuery({
    queryKey: projectKey(scope.workspace, user), queryFn: () => listProjects(scope.workspace), enabled: !!scope.project,
  });
  return scope.project ? projects.data?.items.find((project) => project.id === scope.project)?.name ?? scope.project
    : "共享知识";
}

function WorkflowStart({ scope, search, user, workspaceName }: {
  scope: Scope;
  search: URLSearchParams;
  user: string;
  workspaceName: string;
}) {
  const navigate = useNavigate();
  const destination = useDestination(scope, user);
  const workflow = search.get("workflow") === "teamai-share-session" ? "teamai-share-session" : "teamai-share-learnings";
  const intent: KnowledgeWorkflowStart = {
    operation_id: search.get("operation") ?? "", runtime: search.get("runtime") ?? "",
    session: search.get("session") ?? "", hitl: search.get("hitl") === "auto" ? "auto" : "person",
    publish: search.get("publish") === "yes", scorers: search.getAll("scorer"),
  };
  const start = useMutation({
    mutationFn: () => startWorkflow(scope, workflow, intent),
    onSuccess: (result) => {
      const target = new URLSearchParams({ workspace: scope.workspace });
      if (scope.project) target.set("project", scope.project);
      navigate(`/knowledge/workflows/${result.id}?${target}`, { replace: true });
    },
  });
  return <Card>
    <div className="knowledge-form">
      <Typography.Title heading={2}>{workflow === "teamai-share-session" ? "启动会话分享" : "启动会话经验分享"}</Typography.Title>
      <p className="knowledge-text">{workspaceName} · {destination}</p>
      <p className="knowledge-text">{intent.runtime} · {intent.session}</p>
      <p>{intent.publish ? "发布脱敏会话" : "会话保留在本机"}</p>
      {intent.scorers.length > 0 && <p className="knowledge-text">评分标准 · {intent.scorers.join("、")}</p>}
      {start.isError && <Banner closeIcon={null} type="danger" description={errorMessage(start.error)} />}
      <div className="knowledge-actions">
        <Button theme="solid" loading={start.isPending} onClick={() => start.mutate()}>本人启动</Button>
      </div>
    </div>
  </Card>;
}

function WorkflowApproval({ scope, user, workspaceName, run }: {
  scope: Scope;
  user: string;
  workspaceName: string;
  run: string;
}) {
  const cache = useQueryClient();
  const key = ["knowledge-workflow", scope.workspace, user, scope.project, run];
  const loaded = useQuery({ queryKey: key, queryFn: () => workflowRun(scope, run), retry: false });
  const destination = useDestination(scope, user);
  const decide = useMutation({
    mutationFn: ({ step, decision }: { step: string; decision: WorkflowApprovalDecision }) =>
      decideWorkflow(scope, run, step, decision),
    onSuccess: (result) => cache.setQueryData(key, result),
  });
  if (loaded.isPending) return <Skeleton active />;
  if (loaded.isError) return <AssetLoadError title="暂时无法打开审批" description={errorMessage(loaded.error)}
    onRetry={() => void loaded.refetch()} />;

  // 优先处理发起许可，流程步骤中的审批由服务端标记为就绪后才可提交。
  const data = loaded.data;
  const awaitingLaunch = data.launch === "pending";
  const readySteps = data.steps.filter((step) => step.state === "ready");
  const approvalStep = readySteps.find((step) => step.approve);
  const approvalId = awaitingLaunch ? "_launch" : approvalStep?.id;
  const approvalMessage = awaitingLaunch ? "允许当前设备发起会话经验分享？" : approvalStep?.approve?.message;
  const draftPrompt = readySteps.find((step) => step.prompt)?.prompt;
  const awaitingArchive = readySteps.some((step) => step.uses === "weora.share-session");

  return <Card>
    <div className="knowledge-form">
      <Typography.Title heading={2}>{approvalMessage || workflowStatusLabel(data)}</Typography.Title>
      <p className="knowledge-text">
        {workspaceName} · {destination} · 定义版本 {data.revision}
        {data.session_workflow_revision !== null && ` · 会话流程版本 ${data.session_workflow_revision}`}
      </p>
      {awaitingLaunch && <div className="knowledge-form">
        <p className="knowledge-text">{data.device_name} · {data.parameters.runtime} · {data.parameters.session}</p>
        <p>{data.parameters.publish ? "会发布脱敏后的会话" : "会话保持本机可见"}</p>
        {data.parameters.scorers.length > 0 &&
          <p className="knowledge-text">评分标准 · {data.parameters.scorers.join("、")}</p>}
      </div>}
      {data.drafts?.map(({ title, body, namespace }, index) => (
        <section className="knowledge-form" key={`${index}:${title}`}>
          <Typography.Title heading={3}>{title}</Typography.Title>
          {namespace && <p className="knowledge-text">目录 · {namespace}</p>}
          <div className="knowledge-text">{body}</div>
        </section>
      ))}
      {decide.isError && <Banner closeIcon={null} type="danger" description={errorMessage(decide.error)} />}
      {draftPrompt && <WorkflowDraftForm scope={scope} run={run} prompt={draftPrompt}
        onSaved={() => void loaded.refetch()} />}
      {awaitingArchive && <WorkflowArchiveForm scope={scope} run={run} onSaved={() => void loaded.refetch()} />}
      <div className="knowledge-actions">
        {approvalId ? <>
          <Button disabled={decide.isPending}
            onClick={() => decide.mutate({ step: approvalId, decision: "reject" })}>拒绝</Button>
          <Button theme="solid" loading={decide.isPending}
            onClick={() => decide.mutate({ step: approvalId, decision: "approve" })}>同意</Button>
        </> : <Button onClick={() => void loaded.refetch()} loading={loaded.isFetching}>刷新状态</Button>}
      </div>
    </div>
  </Card>;
}

/** 发布结果优先于后续步骤的等待状态，用户能立即确认经验已入库。 */
function workflowStatusLabel(run: KnowledgeWorkflowRun) {
  if (run.launch === "rejected") return "已拒绝发起";
  if (run.published) return `已发布 ${run.published.length} 份经验`;
  const hasUnfinishedSteps = run.steps.some((step) => step.state === "ready" || step.state === "waiting");
  return hasUnfinishedSteps ? "等待后续步骤" : "已结束";
}

function WorkflowArchiveForm({ scope, run, onSaved }: { scope: Scope; run: string; onSaved: () => void }) {
  const [archive, setArchive] = useState("");
  const [revision, setRevision] = useState("");
  const receiptId = archive.trim();
  const receiptRevision = Number(revision);
  const valid = !!receiptId && /^[1-9][0-9]*$/.test(revision) && Number.isSafeInteger(receiptRevision);
  const save = useMutation({
    mutationFn: () => saveWorkflowArchive(scope, run, receiptId, receiptRevision),
    onSuccess: onSaved,
  });
  return <form className="knowledge-form" onSubmit={(event) => {
    event.preventDefault();
    if (valid) save.mutate();
  }}>
    <p>在 CLI 完成已选的会话归档或评分后，登记对应收据。</p>
    <label>归档或评分证据 ID<Input value={archive} onChange={setArchive} maxLength={36}
      required disabled={save.isPending} /></label>
    <label>证据版本<Input value={revision} onChange={setRevision} inputMode="numeric" maxLength={12}
      required disabled={save.isPending} /></label>
    {save.isError && <Banner closeIcon={null} type="danger" description={errorMessage(save.error)} />}
    <div className="knowledge-actions">
      <Button htmlType="submit" loading={save.isPending} disabled={!valid}>登记收据</Button>
    </div>
  </form>;
}

function WorkflowDraftForm({ scope, run, prompt, onSaved }: {
  scope: Scope; run: string; prompt: string; onSaved: () => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const save = useMutation({
    mutationFn: (skip: boolean) => saveWorkflowDrafts(scope, run, skip ? [] : [{ title: title.trim(), body }]),
    onSuccess: onSaved,
  });
  return <form className="knowledge-form" onSubmit={(event) => { event.preventDefault(); save.mutate(false); }}>
    <p className="knowledge-text">{prompt}</p>
    <label>草稿标题<Input value={title} onChange={setTitle} maxLength={160} required disabled={save.isPending} /></label>
    <label>草稿正文<TextArea value={body} onChange={setBody} maxLength={64000} rows={5}
      required disabled={save.isPending} /></label>
    {save.isError && <Banner closeIcon={null} type="danger" description={errorMessage(save.error)} />}
    <div className="knowledge-actions">
      <Button disabled={save.isPending} onClick={() => save.mutate(true)}>没有经验要分享</Button>
      <Button htmlType="submit" theme="solid" loading={save.isPending} disabled={!title.trim() || !body.trim()}>
        确认草稿
      </Button>
    </div>
  </form>;
}
