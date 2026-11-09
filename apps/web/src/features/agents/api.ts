export type AgentSkillBinding = {
  skill_version_id: string;
  skill_id: string;
  skill_slug: string;
  skill_display_name: string;
  version: string;
  content_sha256: string;
};

export type AgentDraft = {
  system_prompt: string;
  skills: AgentSkillBinding[];
  updated_at: string;
};

export type AgentSummary = {
  id: string;
  display_name: string;
  description: string;
  status: "active" | "archived";
  latest_published_version: string | null;
  updated_at: string;
};

export type AgentDetail = AgentSummary & {
  draft: AgentDraft | null;
  created_at: string;
};

export type AgentVersion = {
  id: string;
  version: string;
  changelog: string;
  display_name: string;
  description: string;
  system_prompt: string;
  skills: AgentSkillBinding[];
  published_by: string;
  published_at: string;
};

export type AgentDraftInput = {
  system_prompt: string;
  skill_version_ids: string[];
};

export type CreateAgentInput = AgentDraftInput & {
  display_name: string;
  description: string;
};

type ErrorBody = {
  detail?:
    | { code?: string; message?: string }
    | Array<{ loc?: Array<string | number>; msg?: string }>;
};

export class AgentsApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "AgentsApiError";
  }
}

function readCookie(name: string): string | undefined {
  const prefix = `${encodeURIComponent(name)}=`;
  const value = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
    ?.slice(prefix.length);
  return value ? decodeURIComponent(value) : undefined;
}

async function request<T>(
  workspaceId: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(
    `/api/v1/workspaces/${workspaceId}/agents${path}`,
    {
      ...init,
      credentials: "include",
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
    },
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    const detail = body.detail;
    const isValidationError = Array.isArray(detail);
    throw new AgentsApiError(
      isValidationError
        ? "请检查填写内容后重试"
        : (detail?.message ?? "请求未完成，请稍后重试"),
      response.status,
      isValidationError ? undefined : detail?.code,
    );
  }
  return (await response.json()) as T;
}

function writeHeaders(): HeadersInit {
  const csrfToken = readCookie("weora_csrf");
  return {
    "Content-Type": "application/json",
    ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
  };
}

export function listAgents(
  workspaceId: string,
): Promise<{ items: AgentSummary[] }> {
  return request(workspaceId, "");
}

export function createAgent(
  workspaceId: string,
  input: CreateAgentInput,
): Promise<AgentDetail> {
  return request(workspaceId, "", {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

export function getAgent(
  workspaceId: string,
  agentId: string,
): Promise<AgentDetail> {
  return request(workspaceId, `/${agentId}`);
}

export function updateAgentDraft(
  workspaceId: string,
  agentId: string,
  input: AgentDraftInput,
): Promise<AgentDraft> {
  return request(workspaceId, `/${agentId}/draft`, {
    method: "PATCH",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

export function publishAgent(
  workspaceId: string,
  agentId: string,
  input: { version: string; changelog: string },
): Promise<AgentVersion> {
  return request(workspaceId, `/${agentId}/publish`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

export function listAgentVersions(
  workspaceId: string,
  agentId: string,
): Promise<{ items: AgentVersion[] }> {
  return request(workspaceId, `/${agentId}/versions`);
}
