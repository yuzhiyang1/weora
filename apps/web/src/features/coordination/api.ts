import { ConsoleApiError, consoleRequest, consoleWrite, errorMessage } from "../runs/api";

/** 将可操作的协调错误翻译给管理员，其余错误继续使用控制台统一处理。 */
export function coordinationError(error: unknown): string {
  const messages: Record<string, string> = {
    CONFIG_REVISION_CONFLICT: "配置已被更新，请关闭弹窗并刷新后重试。",
    ACTOR_UNBOUND: "所选账号尚未绑定有效成员或未获得来源授权。",
    CODE_SOURCE_NOT_CONFIGURED: "仓库尚未登记可用的托管连接，请先在代码设置中配置。",
    CODE_SOURCE_CAPABILITY_UNSUPPORTED: "当前代码托管连接不支持所选事件类型。",
    REMOTE_BINDING_CHANGED: "仓库连接已变化，请重新选择来源并确认授权。",
  };
  const message = error instanceof ConsoleApiError && error.code ? messages[error.code] : undefined;
  return message ?? errorMessage(error);
}

export type CoordinationRule = {
  id: string;
  name: string;
  instruction: string;
  enabled: boolean;
};
export type CoordinationConfig = {
  id: string;
  source_id: string;
  route_key: string;
  main_agent_id: string;
  main_agent_version: string;
  worker_agent_id: string;
  worker_agent_version: string;
  mode: "off" | "observe" | "auto";
  enabled: boolean;
  paused: boolean;
  allowed_actors: string[];
  allowed_bots?: string[];
  rules: CoordinationRule[];
  revision: number;
  max_workers: number;
  main_model_id: string | null;
  worker_model_id: string | null;
  source_kind?: "feishu" | "code_hosting";
  project_id?: string | null;
  repository_source?: {
    repository_id: string;
    remote_id: string;
    agent_login: string;
    actors: Record<string, string>;
    observe_issues: boolean;
    observe_pulls: boolean;
    observe_comments: boolean;
    observe_ci: boolean;
  } | null;
};
export type CoordinationTicket = {
  id: string;
  title: string;
  outcome: string | null;
  attention: string | null;
  assignment: {
    id: string;
    run_id: string | null;
    report: { state: string; summary: string } | null;
  } | null;
};
export type CoordinationScope = {
  config: CoordinationConfig;
  status: {
    tickets?: CoordinationTicket[];
    activities?: { id: string; run_id: string; kind: string; state: string }[];
    sessions?: {
      id: string;
      role: string;
      model_id: string | null;
      model_retry: { attempt: number; delay_ms: number } | null;
    }[];
    evaluations?: { id: string; primary: string; state: string }[];
  };
  effects: {
    kind: string;
    state: string;
    attempts: number;
    error: string | null;
  }[];
  deliveries: {
    id: string;
    purpose: string;
    state: string;
    remote_message_id: string | null;
  }[];
};
export type CoordinationResource =
  | "threads"
  | "decisions"
  | "activities"
  | "effects"
  | "sessions"
  | "notices";
export type CoordinationRecord = {
  id: string;
  state?: string;
  kind?: string;
  reason?: string;
  summary?: string;
  subscriptions?: string[];
  pending?: string[];
  due?: string | null;
  model_id?: string | null;
  model_retry?: { attempt: number; delay_ms: number } | null;
  result?: { text?: string; code?: string };
  data?: Record<string, unknown>;
};
export type RecordsPage = {
  items?: CoordinationRecord[];
  notices?: CoordinationRecord[];
  sessions?: CoordinationRecord[];
  next_offset?: number | null;
};

export const listCoordination = (workspaceId: string) =>
  consoleRequest<{ items: CoordinationScope[] }>(workspaceId, "/coordination");

/** 原配置的其余准入字段由服务端响应带回；只排除派生标识，保留管理员已确认的行为。 */
export const saveCoordination = (
  workspaceId: string,
  config: CoordinationConfig,
) => {
  // 白名单对应 ConfigInput，避免把服务端项目快照或审计字段当作可写配置。
  const allowed = [
    "source_id",
    "route_key",
    "main_agent_id",
    "main_agent_version",
    "worker_agent_id",
    "worker_agent_version",
    "allowed_actors",
    "allowed_bots",
    "rules",
    "mode",
    "enabled",
    "paused",
    "observe_unmentioned",
    "max_workers",
    "eval_timeout_seconds",
    "debounce_ms",
    "listed_unaddressed",
    "unlisted_addressed",
    "unlisted_unaddressed",
    "main_model_id",
    "worker_model_id",
  ];
  const values = Object.fromEntries(
    Object.entries(config).filter(([key]) => allowed.includes(key)),
  );
  if (config.source_kind === "code_hosting" && config.repository_source) {
    delete values.source_id;
    delete values.route_key;
    const source = config.repository_source;
    return consoleWrite(
      workspaceId,
      "/coordination/code-config",
      {
        ...values,
        expected_revision: config.revision,
        project_id: config.project_id ?? null,
        repository_id: source.repository_id,
        remote_id: source.remote_id,
        agent_login: source.agent_login,
        actors: source.actors,
        observe_issues: source.observe_issues,
        observe_pulls: source.observe_pulls,
        observe_comments: source.observe_comments,
        observe_ci: source.observe_ci,
      },
      "PUT",
    );
  }
  return consoleWrite(
    workspaceId,
    "/coordination/config",
    { ...values, expected_revision: config.revision },
    "PUT",
  );
};
export type SetupOptions = {
  user_id: string;
  agents: { agent_id: string; version: string; label: string }[];
  routes: {
    source_id: string;
    route_key: string;
    label: string;
    worker_agent_id: string;
    worker_agent_version: string;
    actors: string[];
  }[];
  repositories: {
    repository_id: string;
    remote_id: string;
    project_id: string | null;
    provider: string;
    label: string;
  }[];
};
export const getSetupOptions = (workspaceId: string) =>
  consoleRequest<SetupOptions>(workspaceId, "/coordination/setup-options");
export const getCoordinationRecords = (
  workspaceId: string,
  scopeId: string,
  resource: CoordinationResource,
  offset: number,
) =>
  consoleRequest<RecordsPage>(
    workspaceId,
    `/coordination/${encodeURIComponent(scopeId)}/records?resource=${resource}&offset=${offset}`,
  );
