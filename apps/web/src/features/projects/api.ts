import { consoleRequest, consoleWrite } from "../runs/api";

export type ProjectInput = {
  name: string;
  description: string;
  architecture: string;
  conventions: string;
};
export type Project = ProjectInput & {
  id: string;
  version: number;
  status: "active" | "archived";
  can_manage: boolean;
  updated_at: string;
};
export type RepositoryInput = {
  name: string;
  purpose: string;
  provider: "git" | "codeup" | "gitlab" | "github";
  git_url: string;
  default_branch: string;
  technology: string;
  development_notes: string;
  status: "active" | "disabled";
  // 沿用既有接口字段名；HTTPS 将其作为密码传递，兼容 Git 密码和访问令牌。
  credential_token?: string;
  credential_username?: string;
  code_source_id?: string | null;
  platform_repository?: string;
  build_config?: { profile: "default" | "project"; commands: string[][] } | null;
};
export type Repository = Omit<RepositoryInput, "credential_token"> & {
  id: string;
  project_id: string;
  version: number;
  has_credential: boolean;
  connection_status: "registered" | "connected" | "failed";
  connection_check?: {
    status: string;
    checked_at: string;
    revision?: string;
    error_code?: string;
    message?: string;
  } | null;
  execution_status: "not_prepared" | "pending_readonly";
  code_source_version?: number | null;
};
export type ProjectDetail = Project & { repositories: Repository[] };
export type ProjectSnapshot = {
  read_protocol?: "git-objects-v1";
  project_id: string;
  name: string;
  version: number;
  description: string;
  architecture: string;
  conventions: string;
  repositories: Array<
    Pick<
      Repository,
      | "id"
      | "name"
      | "purpose"
      | "provider"
      | "git_url"
      | "default_branch"
      | "technology"
      | "development_notes"
      | "version"
      | "execution_status"
    >
  >;
};
export const projectKey = (workspace: string, user: string, id?: string) => [
  "projects",
  workspace,
  user,
  ...(id !== undefined ? ["detail", id] : []),
];
// 明确投影表单字段，不能把详情对象里的版本、权限或仓库列表作为写入参数发送。
const projectInput = (input: ProjectInput): ProjectInput => ({
  name: input.name,
  description: input.description,
  architecture: input.architecture,
  conventions: input.conventions,
});
export const listProjects = (workspace: string) =>
  consoleRequest<{ items: Project[] }>(workspace, "/projects");
export const getProject = (workspace: string, id: string) =>
  consoleRequest<ProjectDetail>(workspace, `/projects/${id}`);
export const createProject = (workspace: string, input: ProjectInput) =>
  consoleWrite<ProjectDetail>(workspace, "/projects", projectInput(input));
export const editProject = (
  workspace: string,
  project: Project,
  input: ProjectInput,
  status = project.status,
) =>
  consoleWrite<ProjectDetail>(
    workspace,
    `/projects/${project.id}`,
    { ...projectInput(input), version: project.version, status },
    "PUT",
  );
export const saveRepository = (
  workspace: string,
  project: string,
  input: RepositoryInput,
  current?: Repository,
) =>
  consoleWrite<ProjectDetail>(
    workspace,
    `/projects/${project}/repositories${current ? `/${current.id}` : ""}`,
    { ...input, ...(current ? { version: current.version } : {}) },
    current ? "PUT" : "POST",
  );

export const checkRepository = (
  workspace: string,
  project: string,
  repository: Repository,
) =>
  consoleWrite<{
    status: string;
    revision?: string;
    error_code?: string;
    message?: string;
  }>(workspace, `/projects/${project}/repositories/${repository.id}/check`, {
    version: repository.version,
  });

export type RunRepository = {
  repository_id: string;
  // 默认分支解析失败时还没有固定提交。
  revision: string | null;
  status: "PREPARING" | "READY" | "FAILED";
  error_code: string | null;
};
