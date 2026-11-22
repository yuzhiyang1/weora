import { consoleRequest, consoleWrite } from "../runs/api";

export const knowledgeTypes = { learning: "经验", doc: "文档", rule: "规则", code: "代码", skill: "Skill" } as const;
export type KnowledgeKind = keyof typeof knowledgeTypes;
export type PromotionKind = "rule" | "doc" | "skill";
export type Entry = {
  id: string;
  kind: string;
  title: string;
  project_id: string | null;
  revision: number | string;
  source: { type: string; ref: string; merge_commit?: string };
  body?: string;
  relations: { kind: string; target: string; title: string; direction: "in" | "out" }[];
};
export type HealthEntry = Pick<Entry, "id" | "title" | "kind"> & {
  confidence: number;
  recalls: number;
  upvotes: number;
  recallers: number;
};
export type HealthReport = {
  generated_at: string;
  summary: {
    entries: number; recalls: number; upvotes: number; coverage: number;
    contributors: number; hot: number; cold: number; silent: number;
  };
  coverage: { kind: string; total: number; covered: number; percent: number }[];
  top_recalled: HealthEntry[];
  silent: HealthEntry[];
  last_recall_months: { month: string; entries: number }[];
  authors: { id: string; name: string; entries: number; recalls: number }[];
  graph: { relations: number; unresolved: number; without_relations: number; silent_without_relations: number };
  maintenance: Record<"promote" | "prune" | "quality", { total: number; items: HealthEntry[] }>;
  code_sources: {
    id: string; name: string; source: "repository" | "local_directory";
    freshness: "unknown" | "current" | "stale"; indexed: number; revision?: string;
    truncated?: boolean;
  }[];
};
export type Scope = { workspace: string; project: string };
export type Page<T> = { items: T[]; next_after?: string | null };
export type MaintenanceType = "promote" | "prune" | "quality";
export type Candidate = {
  id: string; title: string; confidence?: number; recalls: number; upvotes: number;
  prune_reason?: string; status?: string;
};
export type PromotionContext = {
  id: string; revision: number; title: string; body: string; kind: PromotionKind;
  targets: Record<PromotionKind, string>; occupied_kinds: PromotionKind[];
};
export type QualityContext = {
  id: string; title: string; body: string; revision?: number; skill_version_id?: string;
};
export type CodeSource = {
  repository_id?: string; codebase_id?: string; source?: "local_directory"; name?: string;
  freshness: "unknown" | "current" | "stale"; indexed: number; revision?: string;
  review_sources?: { url: string; revision: string; indexed_at: string }[];
};
export const workflowNames = ["teamai-share-learnings", "teamai-share-session"] as const;
export type WorkflowName = typeof workflowNames[number];
export type WorkflowDefinition = {
  revision: number; overridden: boolean;
  definition: { invoke: "allow" | "ask" | "deny"; alias?: string; description: string };
};
export type WorkflowApprovalDecision = "approve" | "reject";
export type KnowledgeWorkflowRun = {
  id: string;
  revision: number;
  session_workflow_revision: number | null;
  device_name: string | null;
  launch: "pending" | "approved" | "rejected";
  parameters: { runtime: string; session: string; publish: boolean; scorers: string[] };
  steps: {
    id: string;
    state: "waiting" | "ready" | "success" | "skipped" | "rejected";
    approve?: { message: string; required: boolean };
    prompt?: string;
    uses?: "weora.contribute-learnings" | "weora.share-session";
  }[];
  drafts: { title: string; body: string; namespace: string | null }[] | null;
  published: { id: string; revision: number }[] | null;
};

function path(scope: Scope, suffix: string, params?: Record<string, string>) {
  const search = new URLSearchParams(params);
  if (scope.project) search.set("project_id", scope.project);
  return `/knowledge${suffix}${search.size ? `?${search}` : ""}`;
}

export const list = (scope: Scope, review: boolean, query: string, kind: string, prOnly: boolean) =>
  consoleRequest<Page<Entry>>(scope.workspace, path(scope, review ? "/review" : "", review ? undefined : {
    ...(query ? { query } : {}), ...(kind ? { kind } : {}), ...(prOnly ? { source_type: "github_pr" } : {}),
    limit: "100",
  }));
export const detail = (scope: Scope, id: string) =>
  consoleRequest<Entry>(scope.workspace, path(scope, `/${encodeURIComponent(id)}`));
export const health = (scope: Scope) =>
  consoleRequest<HealthReport>(scope.workspace, path(scope, "/health"));
export const codeSources = (scope: Scope) =>
  consoleRequest<Page<CodeSource>>(scope.workspace, path(scope, "/codebases"));
export const feedback = (scope: Scope, id: string, session: string, positive: boolean) =>
  consoleWrite<void>(scope.workspace, path(scope, `/${encodeURIComponent(id)}/feedback`), { session, positive });
export function maintenance(scope: Scope, kind: MaintenanceType, after: string) {
  const suffix = kind === "prune" ? "/maintenance" : `/maintenance/${kind}`;
  return consoleRequest<Page<Candidate>>(scope.workspace, path(scope, suffix, {
    ...(kind === "prune" ? { candidates_only: "true" } : {}), ...(after ? { after } : {}),
  }));
}
export const archive = (scope: Scope, id: string) =>
  consoleWrite<Page<Candidate>>(scope.workspace, path(scope, "/maintenance/archive", { entry_id: id }), {});
export const promotionPreview = (scope: Scope, id: string) =>
  consoleRequest<PromotionContext>(scope.workspace, path(scope, `/maintenance/promote/${encodeURIComponent(id)}`));
export const qualityPreview = (scope: Scope, id: string) =>
  consoleRequest<QualityContext>(scope.workspace, path(scope, `/maintenance/quality/${encodeURIComponent(id)}`));
export const submitPromotion = (scope: Scope, id: string, input: {
  operation_id: string; source_revision: number; kind: PromotionKind; reason: string; body: string;
}) => consoleWrite<Entry>(scope.workspace, path(scope, `/maintenance/promote/${encodeURIComponent(id)}`), input);
export const submitQuality = (scope: Scope, id: string, input: {
  operation_id: string; base_revision?: number; base_skill_version_id?: string; body: string;
}) => consoleWrite<Entry>(scope.workspace, path(scope, `/maintenance/quality/${encodeURIComponent(id)}`), input);
export const workflowDefinition = (workspace: string, name: WorkflowName) =>
  consoleRequest<WorkflowDefinition>(workspace, `/knowledge/workflows/${name}`);
export const saveWorkflowDefinition = (workspace: string, name: WorkflowName, base_revision: number, definition: string | null) =>
  consoleWrite<WorkflowDefinition>(workspace, `/knowledge/workflows/${name}`, { base_revision, definition }, "PUT");
export const workflowRun = (scope: Scope, run: string) =>
  consoleRequest<KnowledgeWorkflowRun>(scope.workspace, path(scope, `/workflow-runs/${encodeURIComponent(run)}`));
export type KnowledgeWorkflowStart = {
  operation_id: string; runtime: string; session: string; hitl: string; publish: boolean; scorers: string[];
};
export const startWorkflow = (scope: Scope, name: string, input: KnowledgeWorkflowStart) =>
  consoleWrite<KnowledgeWorkflowRun>(
    scope.workspace, path(scope, `/workflows/${encodeURIComponent(name)}/runs`), input,
  );
export const saveWorkflowDrafts = (scope: Scope, run: string, items: { title: string; body: string }[]) =>
  consoleWrite<KnowledgeWorkflowRun>(
    scope.workspace, path(scope, `/workflow-runs/${encodeURIComponent(run)}/drafts`), { items }, "PUT",
  );
export const saveWorkflowArchive = (scope: Scope, run: string, archive_id: string, revision: number) =>
  consoleWrite<KnowledgeWorkflowRun>(
    scope.workspace, path(scope, `/workflow-runs/${encodeURIComponent(run)}/session`),
    { archive_id, revision }, "PUT",
  );
export const decideWorkflow = (scope: Scope, run: string, step: string, decision: WorkflowApprovalDecision) =>
  consoleWrite<KnowledgeWorkflowRun>(
    scope.workspace, path(scope, `/workflow-runs/${encodeURIComponent(run)}/approvals/${encodeURIComponent(step)}`),
    { decision }, "PUT",
  );
export const contribute = (scope: Scope, value: { operation_id: string; title: string; body: string; ref: string }) =>
  consoleWrite<Entry>(scope.workspace, path(scope, ""), {
    operation_id: value.operation_id, title: value.title, body: value.body,
    source: { type: "manual", ref: value.ref },
  });
export const review = async (scope: Scope, entry: Entry, decision: "accept" | "reject") => {
  // SemVer 属于 Registry 已发布版本，知识评审只处理数字版本的贡献。
  if (typeof entry.revision !== "number") throw new Error("该版本由 Skill Registry 管理");
  return consoleWrite<Entry>(scope.workspace, path(scope, `/${encodeURIComponent(entry.id)}/review`), {
    revision: entry.revision, decision,
  });
};
