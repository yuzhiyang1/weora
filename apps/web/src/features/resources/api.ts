import { authWrite } from "../auth/api";

export type TeamRule = {
  name: string;
  path: string;
  namespace: string | null;
  description: string;
  body: string;
  paths: string[];
  tags: string[];
};

export type ResourceScope = { id: string; name: string; knowledge: string[] };
export type ResourceSnapshot = {
  revision: string | null;
  rules: TeamRule[];
  projects: ResourceScope[];
  roles: ResourceScope[];
  member_roles: string[];
  members_configured: boolean;
};

export type ResourceChange = { id: string; branch: string; revision: string };
export type ResourceChangeDetail = ResourceChange & {
  changes: { path: string; before: string | null; after: string | null }[];
};

async function request<T>(workspace: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/workspaces/${encodeURIComponent(workspace)}/team-resources${path}`, {
    ...init,
    credentials: "include",
  });
  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as { detail?: { message?: string } } | null;
    throw new Error(error?.detail?.message ?? "团队规则请求失败，请重试。");
  }
  return (await response.json()) as T;
}

export const getSnapshot = (workspace: string, signal?: AbortSignal) =>
  request<ResourceSnapshot>(workspace, "/snapshot", { signal: signal ?? null });
export const listChanges = (workspace: string) => request<{ items: ResourceChange[] }>(workspace, "/changes");
export const getChange = (workspace: string, id: string) =>
  request<ResourceChangeDetail>(workspace, `/changes/${encodeURIComponent(id)}`);
export const mergeChange = (workspace: string, change: ResourceChange) =>
  request<ResourceChange>(workspace, `/changes/${encodeURIComponent(change.id)}/merge`,
    authWrite("POST", { expected: change.revision }));
