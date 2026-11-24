import type { HealthReport, Scope } from "../knowledge/api";
import { consoleRequest, consoleWrite } from "../runs/api";

export type PageKind = "improvement" | "self-improvement" | "scorers" | "benchmarks";
type Page<T> = { total: number; next_offset: number | null; items: T[] };
export type Period = {
  from_day: string; through_day: string; sessions_ended: number; sessions_with_correction: number;
  success_rate: number | null; correction_rate: number | null; cache_read_share: number | null;
  prompts: number; avg_prompts: number | null; known_cost_micros: number | null;
  daily_known_cost_micros: number | null; priced_requests: number;
  interrupts: number; tool_rejections: number; corrections: number; sessions_with_intervention: number;
};
export type Day = Period & { day: string; active_members: number };
export type AdoptionSkill = {
  id: string; slug: string; name: string; uses: number; members: number; last_used_at: string | null;
};
export type ImprovementReport = {
  generated_at: string; current: Period; previous: Period; daily: Day[];
  skill_adoption: { since: string; until: string } & Record<
    "adopted" | "unused" | "archived_in_use", { total: number; items: AdoptionSkill[] }
  >;
  maintenance: HealthReport["maintenance"];
};

export type Counts = { total: number; passed: number; failed: number; pass_rate: number | null };
type Measure = {
  scorer: string; scorer_id: string; scorer_revision: number; source_hash: string | null;
  evidence: string; run_mode: string; driver: string; requested_model: string | null;
};
export type ScoreMeasure = Measure & {
  stats: Counts; weeks: (Counts & { week: string })[];
  labels: { value: string; score: number; count: number }[];
  by_runtime: (Counts & { runtime: string })[];
  agreement?: { paired: number; agree: number };
};
export type ScoreFailure = Measure & {
  label_id: number; runtime: string; label: string; score: number; scored_at: string;
  git_start: { status: string };
};
export type ScorerReport = {
  window: { days: number; since: string; until: string };
  cohorts: ScoreMeasure[]; failures: ScoreFailure[]; total_failed: number; truncated: boolean;
};

export type WorkflowGroupBy =
  "tool" | "model" | "team_revision" | "definition" | "role" | "project" | "tag" | "fingerprint" | "skill" | "stage";
export type WorkflowDecider = "check" | "ci" | "protected" | "judge";
type ModelSource = "reported" | "requested" | null;
export type WorkflowMeasure = {
  id: string;
  scorer: string;
  source_hash: string;
  team_revision: string | null;
  run_mode: string | null;
  judge: { kind: "model" | "human" | "stage"; tool: string | null; model: string | null; model_source: ModelSource };
  stats: Counts;
};
export type WorkflowGroup = {
  id: string;
  kind: "value" | "none" | "unknown";
  value: string | null;
  model_source: ModelSource;
  local_changes: boolean;
  role_source: "step" | "member" | "both" | null;
  stats: Counts;
};
export type WorkflowScoreReport = {
  window: ScorerReport["window"];
  measures: WorkflowMeasure[];
  selected: (WorkflowMeasure & Pick<ScoreMeasure, "weeks" | "labels"> & {
    stages: WorkflowGroup[];
    before_judge: number;
    model_stats: Counts;
    groups: WorkflowGroup[];
    active_group: string | null;
    failures: {
      total: number;
      items: {
        id: string; label: string; score: number; run_id: string; step: string; attempt: number;
        scored_at: string; decided_by: WorkflowDecider | null; tool: string | null;
      }[];
    };
  }) | null;
};
export type WorkflowScoreImportResult = { recorded: number; unchanged: number; skipped_sessions: number };
export type WorkflowScoreSyncStatus = {
  available: boolean;
  status: "unconfigured" | "unavailable" | "disabled" | "queued" | "syncing" | "failed" | "ready" | "source_changed";
  branch: string;
  version: number;
  repository_id?: string;
  repository_name?: string | null;
  source_project_id?: string;
  last_synced_at?: string | null;
  revision?: string | null;
  error_code?: string | null;
};
export type WorkflowScoreSyncSettings = {
  expected_version: number; enabled: boolean; source_project_id?: string; repository_id?: string;
};

export type Proof = {
  run_id: string; status: RunStatus; gate: RunGate; verified: boolean;
  comparisons: { task: string; tool: string; compared: number; main_passed: number; pulled_passed: number }[];
};
export type Effect = {
  status: "insufficient_evidence" | "measured";
  cohorts: {
    scorer_revision: number; runtime: string; evidence: string; run_mode: string; driver: string;
    requested_model: string | null; before: Counts; after: Counts;
  }[];
};
export type Proposal = {
  id: string; suite: string; suite_id: string; scorer: string; status: "open" | "merged" | "closed";
  created_at: string; settled_at: string | null; revision: number | null; reason: string | null;
  resources: string[]; tasks: number | null; pr_number: number | null; merge_commit: string | null;
  proof: Proof | null; effect: Effect | null; ci: { run: string; attempt: string | null } | null;
};
export type SelfImprovementReport = Page<Proposal> & {
  scorers: string[];
  summary: { failed_labels: number; tasks: number; open: number; merged: number; closed: number };
};
export type FailureCandidates = {
  subjects: {
    subject_id: string; task: string | null;
    latest: Pick<ScoreFailure, "label" | "runtime" | "scored_at" | "git_start">;
    recorded: { git_start: { status: string } } | null;
    learnings: { id: string; title: string }[];
  }[];
};

export type Arm = "main" | "pulled";
export type RunStatus = "complete" | "expired" | "pending";
export type RunGate = "held" | "incomplete" | "regressed";
type Rate = { planned: number; passed: number; pass_rate: number | null; interval95: [number, number] | null };
export type ArmRate = Rate & { tool: string; arm: Arm; judged: number };
export type BenchmarkPair = {
  task: string; starting_commit: string; tool: string; planned: number; compared: number;
  excluded_judge_changed: number; unjudged: number; main: Rate; pulled: Rate;
};
export type BenchmarkReport = {
  days: number; since: string; until: string; suite_id: string | null;
  suites: { id: string; path: string; description: string; tools: string[]; repetitions: number }[];
  total: number; next_offset: number | null;
  runs: {
    id: string; proposal_id: string | null; base_resources: string; proposal_resources: string;
    ended_at: string; status: RunStatus; arms: ArmRate[];
  }[];
  arms: ArmRate[]; tasks: (ArmRate & { task: string })[]; comparisons: BenchmarkPair[];
  revisions: (ArmRate & { revision: string })[];
};
export type BenchmarkRun = {
  id: string; suite: string; status: RunStatus; gate: RunGate; proposal_id: string | null;
  base_resources: string; proposal_resources: string; created_at: string;
  trials: {
    id: string; task: string; starting_commit: string; tool: string; arm: Arm; repetition: number;
    outcome: string; judge_tool: string | null; judge_model: string | null; judge_effort: string | null;
  }[];
};

/** 所有报表与详情沿用控制台会话和项目鉴权。 */
function query<T>(scope: Scope, path: string, filters: Record<string, string> = {}) {
  const params = new URLSearchParams(filters);
  if (scope.project) params.set("project_id", scope.project);
  return consoleRequest<T>(scope.workspace, "/knowledge/" + path + (params.size ? "?" + params.toString() : ""));
}

export const readImprovement = (scope: Scope, days: string) =>
  query<ImprovementReport>(scope, "dashboard/improvement", { days });
export const readScorers = (scope: Scope, days: string) =>
  query<ScorerReport>(scope, "dashboard/scorers", { days });
export const readWorkflowScores = (scope: Scope, filters: Record<string, string>) =>
  query<WorkflowScoreReport>(scope, "dashboard/scorers/workflow", filters);
export const readWorkflowScoreSync = (scope: Scope) =>
  query<WorkflowScoreSyncStatus>(scope, "dashboard/scorers/workflow/sync");
export function writeWorkflowScoreSync(scope: Scope, input: WorkflowScoreSyncSettings) {
  const project = scope.project ? "?" + new URLSearchParams({ project_id: scope.project }) : "";
  return consoleWrite<WorkflowScoreSyncStatus>(
    scope.workspace, "/knowledge/dashboard/scorers/workflow/sync" + project, input, "PUT",
  );
}
export function uploadWorkflowScores(scope: Scope, jsonl: string) {
  const project = scope.project ? "?" + new URLSearchParams({ project_id: scope.project }) : "";
  return consoleWrite<WorkflowScoreImportResult>(
    scope.workspace, "/knowledge/dashboard/scorers/workflow/import" + project, { jsonl },
  );
}
export const readProposals = (scope: Scope, scorer: string, offset: number) =>
  query<SelfImprovementReport>(scope, "dashboard/self-improvement", {
    ...(scorer ? { scorer } : {}), offset: String(offset),
  });
export const readFailureCandidates = (scope: Scope, scorer: string) =>
  query<FailureCandidates>(scope, "self-improvement/candidates", { scorer });
export const readBenchmarks = (scope: Scope, days: string, suite: string, offset: number) =>
  query<BenchmarkReport>(scope, "dashboard/benchmarks", {
    days, ...(suite ? { suite_id: suite } : {}), offset: String(offset),
  });
export const readBenchmarkRun = (scope: Scope, id: string) =>
  query<BenchmarkRun>(scope, "benchmark/runs/" + encodeURIComponent(id));
