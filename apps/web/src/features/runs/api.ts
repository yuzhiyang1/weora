/** 控制台只传业务参数；工作空间、发起人、凭据和租约由平台确定。 */
import type { ProjectSnapshot } from "../projects/api";
export type RunStatus =
  | "QUEUED"
  | "RUNNING"
  | "WAITING_CONTEXT"
  | "WAITING_APPROVAL"
  | "RECOVERY_REQUIRED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";
export type RunSummary = {
  id: string;
  task_id: string;
  agent_version_id: string;
  agent_name: string;
  agent_version: string;
  task_title?: string | null;
  status: RunStatus;
  last_sequence: number;
  created_at: string;
  updated_at: string;
};
export type RunDetail = RunSummary & {
  instruction: string;
  audience: string;
  output_text: string | null;
  failure_code: string | null;
  cancellation_requested_at: string | null;
  recovery_read_only?: boolean;
  context_cutoff: string;
  project_context?: ProjectSnapshot | null;
  execution_profile: {
    runtime_id: string;
    provider: string;
    model_id: string;
    max_tokens: number | null;
    timeout_seconds: number;
  } | null;
};
export type RunEvent = {
  run_id: string;
  sequence: number;
  event_type: string;
  request_id: string;
  occurred_at: string;
};
export type RunInput = {
  agent_id: string;
  agent_version: string;
  instruction: string;
  project_id?: string;
  repository_ids?: string[];
};
export type RunPage = { items: RunSummary[]; next_before_id: string | null };
export type EventPage = {
  items: RunEvent[];
  next_after: number;
  latest_sequence: number;
  has_more: boolean;
};

export class ConsoleApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "ConsoleApiError";
  }
}

/** 写操作沿用会话 Cookie + CSRF，不把登录令牌拼入 URL。 */
function writeHeaders(): Record<string, string> {
  const value = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("weora_csrf="))
    ?.slice("weora_csrf=".length);
  return {
    "Content-Type": "application/json",
    ...(value ? { "X-CSRF-Token": decodeURIComponent(value) } : {}),
  };
}

export async function consoleRequest<T>(
  workspaceId: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}${path}`,
    {
      ...init,
      credentials: "include",
      headers: { Accept: "application/json", ...init?.headers },
    },
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { message?: string; code?: string } | unknown[];
    };
    const detail = body.detail;
    throw new ConsoleApiError(
      Array.isArray(detail)
        ? "请检查填写内容与权限后重试。"
        : (detail?.message ?? "请求未完成，请稍后重试。"),
      response.status,
      Array.isArray(detail) ? undefined : detail?.code,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function consoleWrite<T>(
  workspaceId: string,
  path: string,
  body: unknown,
  method = "POST",
  headers?: Record<string, string>,
): Promise<T> {
  return consoleRequest(workspaceId, path, {
    method,
    headers: { ...writeHeaders(), ...headers },
    body: JSON.stringify(body),
  });
}

export function listRuns(
  workspaceId: string,
  before?: string,
): Promise<RunPage> {
  return consoleRequest(
    workspaceId,
    `/runs?limit=20${before ? `&before_id=${encodeURIComponent(before)}` : ""}`,
  );
}
export function getRun(workspaceId: string, runId: string, signal?: AbortSignal): Promise<RunDetail> {
  return consoleRequest(workspaceId, `/runs/${encodeURIComponent(runId)}`, { signal: signal ?? null });
}
export function createRun(
  workspaceId: string,
  input: RunInput,
  key: string,
): Promise<RunDetail> {
  return consoleWrite(workspaceId, "/runs", input, "POST", {
    "Idempotency-Key": key,
  });
}
export function cancelRun(
  workspaceId: string,
  runId: string,
): Promise<RunDetail> {
  return consoleWrite(
    workspaceId,
    `/runs/${encodeURIComponent(runId)}/cancel`,
    {},
  );
}

/** 恢复只读分析不重置预算，不重放不确定的写入或远端动作。 */
export function recoverRun(
  workspaceId: string,
  runId: string,
): Promise<RunDetail> {
  return consoleWrite(
    workspaceId,
    `/runs/${encodeURIComponent(runId)}/recover`,
    { mode: "read_only" },
  );
}
export function getRunEvents(
  workspaceId: string,
  runId: string,
  after: number,
  signal?: AbortSignal,
): Promise<EventPage> {
  return consoleRequest(
    workspaceId,
    `/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=100`,
    { signal: signal ?? null },
  );
}
export const runKey = (workspaceId: string, userId: string, runId?: string) => [
  "runs",
  workspaceId,
  userId,
  ...(runId ? [runId] : []),
];
export const isTerminal = (status: RunStatus) =>
  ["SUCCEEDED", "FAILED", "CANCELLED"].includes(status);
export function errorMessage(error: unknown): string {
  return error instanceof ConsoleApiError
    ? error.message
    : "请求未完成，请检查网络后重试。";
}
