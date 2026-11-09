import { consoleRequest, consoleWrite } from "../runs/api";
import type { Repository } from "./api";

export type CodeSourceInput = {
  name: string;
  provider: "codeup";
  api_origin: string;
  organization: string;
  enabled: boolean;
  api_token?: string;
};
export type CodeSource = Omit<CodeSourceInput, "api_token"> & {
  id: string;
  version: number;
  can_manage: boolean;
  has_credential: boolean;
  verification_status: "not_checked";
};
export type WorkspaceRepository = Repository & {
  project_name: string;
  project_status: "active" | "archived";
  can_manage: boolean;
};
export type BuildProfile = { id: "default" | "project"; name: string };
export type LegacyCodeSource = {
  fingerprint: string;
  repository_id: string;
  repository_name: string;
  repository_version: number;
  organization: string;
};
export const listLegacyCodeSources = (workspace: string) =>
  consoleRequest<{ items: LegacyCodeSource[] }>(
    workspace,
    "/code-sources/legacy",
  );
export const importLegacyCodeSource = (
  workspace: string,
  candidate: LegacyCodeSource,
) =>
  consoleWrite(workspace, "/code-sources/legacy/import", {
    repository_id: candidate.repository_id,
    repository_version: candidate.repository_version,
    fingerprint: candidate.fingerprint,
  });

export const codeSourceKey = (workspace: string, user: string) => [
  "code-sources",
  workspace,
  user,
];
export const listCodeSources = (workspace: string) =>
  consoleRequest<{ items: CodeSource[] }>(workspace, "/code-sources");
export const listWorkspaceRepositories = (workspace: string) =>
  consoleRequest<{ items: WorkspaceRepository[]; truncated: boolean }>(
    workspace,
    "/code-sources/repositories",
  );
export const listBuildProfiles = (workspace: string) =>
  consoleRequest<{ items: BuildProfile[] }>(
    workspace,
    "/code-sources/build-profiles",
  );

export function saveCodeSource(
  workspace: string,
  input: CodeSourceInput,
  current?: CodeSource,
) {
  // 只投影写字段，响应中的权限、版本状态与秘密存在标识不能混入请求。
  return consoleWrite<CodeSource>(
    workspace,
    `/code-sources${current ? `/${current.id}` : ""}`,
    {
      name: input.name,
      provider: input.provider,
      api_origin: input.api_origin,
      organization: input.organization,
      enabled: input.enabled,
      ...(input.api_token !== undefined ? { api_token: input.api_token } : {}),
      ...(current ? { version: current.version } : {}),
    },
    current ? "PUT" : "POST",
  );
}
