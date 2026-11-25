import { skillsPath } from "../resources/routes";

export type SkillFile = {
  path: string;
  size: number;
  media_type: string;
  sha256: string;
};

export type SkillCompatibility = {
  target: string;
  status:
    | "compatible"
    | "compatible_with_warnings"
    | "incompatible"
    | "not_evaluated";
  message: string;
};

export type SkillWarning = {
  code: string;
  message: string;
  path: string | null;
  blocks_publish: boolean;
};

/** 版本包内的展示信息，安装和引用继续使用 slug。 */
export type SkillInterface = {
  display_name: string | null;
  short_description: string | null;
  default_prompt: string | null;
};

export type SkillDraft = {
  package_name: string;
  package_size: number;
  package_sha256: string;
  content_sha256: string;
  files: SkillFile[];
  compatibility: SkillCompatibility[];
  warnings: SkillWarning[];
  updated_at: string;
  uploader_name: string | null;
  interface: SkillInterface | null;
};

export type SkillSummary = {
  id: string;
  slug: string;
  /** 仅多级目录资产返回该字段。 */
  namespace?: string;
  display_name: string;
  description: string;
  status: "active" | "archived";
  latest_published_version: string | null;
  updated_at: string;
  uploader_name: string | null;
};

export type SkillDetail = SkillSummary & {
  draft: SkillDraft | null;
  created_at: string;
  interface: SkillInterface | null;
};

export type SkillVersion = {
  id: string;
  version: string;
  changelog: string;
  package_name: string;
  package_size: number;
  package_sha256: string;
  content_sha256: string;
  files: SkillFile[];
  compatibility: SkillCompatibility[];
  warnings: SkillWarning[];
  published_by: string;
  published_at: string;
  uploader_name: string | null;
  interface: SkillInterface | null;
};

export type SkillContribution = {
  id: string;
  uploader_name: string | null;
  package_name: string;
  created_at: string;
};

/** 预览请求必须明确指定发布版本、当前草稿或候选包。 */
export type SkillFileSource =
  | { version: string }
  | { contributionId: string }
  | { draft: string };

export type SkillEvent = {
  id: string;
  event_type: string;
  actor_id: string;
  /** 下载时的显示名，历史记录可用当前成员名补齐。 */
  actor_name: string | null;
  request_id: string;
  data: Record<string, unknown>;
  occurred_at: string;
};

type ErrorBody = {
  detail?:
    | { code?: string; message?: string }
    | Array<{ loc?: Array<string | number>; msg?: string }>;
};

export class SkillsApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "SkillsApiError";
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

function projectQuery(path: string, projectId?: string): string {
  if (!projectId) return path;
  const [pathname, existing] = path.split("?", 2);
  const params = new URLSearchParams(existing);
  params.set("project_id", projectId);
  return `${pathname}?${params}`;
}

/** 列表和详情沿用同一个可分享的项目范围。 */
export function skillPagePath(projectId?: string, skillId?: string): string {
  const base = skillId ? `${skillsPath}/${encodeURIComponent(skillId)}` : skillsPath;
  return projectId ? `${base}?${new URLSearchParams({ project: projectId })}` : base;
}

async function request<T>(
  workspaceId: string,
  path: string,
  projectId?: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(
    `/api/v1/workspaces/${workspaceId}/skills${projectQuery(path, projectId)}`,
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
    throw new SkillsApiError(
      isValidationError
        ? "请检查填写内容后重试"
        : (detail?.message ?? "请求未完成，请稍后重试"),
      response.status,
      isValidationError ? undefined : detail?.code,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function writeHeaders(contentType?: string): HeadersInit {
  const csrfToken = readCookie("weora_csrf");
  return {
    ...(contentType ? { "Content-Type": contentType } : {}),
    ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
  };
}

export function listSkills(
  workspaceId: string,
  filters: { search?: string; status?: string },
  projectId?: string,
): Promise<{ items: SkillSummary[]; next_cursor: string | null }> {
  const params = new URLSearchParams();
  if (filters.search) params.set("search", filters.search);
  if (filters.status) params.set("status", filters.status);
  const query = params.size ? `?${params.toString()}` : "";
  return request(workspaceId, query, projectId);
}

export function createSkill(
  workspaceId: string,
  file: File,
  projectId?: string,
  namespace?: string,
): Promise<SkillDetail> {
  const form = new FormData();
  form.append("package", file);
  if (namespace) form.append("namespace", namespace);
  return request(workspaceId, "", projectId, {
    method: "POST",
    headers: writeHeaders(),
    body: form,
  });
}

export function getSkill(
  workspaceId: string,
  skillId: string,
  projectId?: string,
): Promise<SkillDetail> {
  return request(workspaceId, `/${skillId}`, projectId);
}

/** 源文件作为 JSON 文本返回，浏览器仅在阅读区渲染。 */
export function previewSkillFile(
  workspaceId: string,
  skillId: string,
  source: SkillFileSource,
  path: string,
  signal: AbortSignal,
  projectId?: string,
): Promise<{ text: string | null }> {
  const params = new URLSearchParams({ path });
  if ("version" in source) params.set("version", source.version);
  if ("contributionId" in source) params.set("contribution_id", source.contributionId);
  if ("draft" in source) params.set("draft_sha256", source.draft);
  return request(workspaceId, `/${skillId}/files/preview?${params}`, projectId, { signal });
}

/** 仅管理员能查询同名上传的待审核包。 */
export function listSkillContributions(workspaceId: string, skillId: string, projectId?: string): Promise<SkillContribution[]> {
  return request(workspaceId, `/${skillId}/contributions`, projectId);
}

/** 复用草稿校验报告显示候选包。 */
export function getSkillContribution(workspaceId: string, skillId: string, id: string, projectId?: string): Promise<SkillDraft> {
  return request(workspaceId, `/${skillId}/contributions/${id}`, projectId);
}

/** 接受时使用服务端修订校验，防止覆盖后来上传的草稿。 */
export function decideSkillContribution(
  workspaceId: string, skillId: string, id: string, accept: boolean, projectId?: string,
): Promise<void> {
  return request(workspaceId, `/${skillId}/contributions/${id}/decision`, projectId, {
    method: "POST",
    headers: writeHeaders("application/json"),
    body: JSON.stringify({ accept }),
  });
}

export function updateSkill(
  workspaceId: string,
  skillId: string,
  input: { display_name: string; description: string },
  projectId?: string,
): Promise<SkillDetail> {
  return request(workspaceId, `/${skillId}`, projectId, {
    method: "PATCH",
    headers: writeHeaders("application/json"),
    body: JSON.stringify(input),
  });
}

export function replaceSkillDraft(
  workspaceId: string,
  skillId: string,
  file: File,
  projectId?: string,
): Promise<SkillDraft> {
  const form = new FormData();
  form.append("package", file);
  return request(workspaceId, `/${skillId}/draft`, projectId, {
    method: "POST",
    headers: writeHeaders(),
    body: form,
  });
}

export function publishSkill(
  workspaceId: string,
  skillId: string,
  input: { version: string; changelog: string; draft_sha256: string },
  projectId?: string,
): Promise<SkillVersion> {
  return request(workspaceId, `/${skillId}/publish`, projectId, {
    method: "POST",
    headers: writeHeaders("application/json"),
    body: JSON.stringify(input),
  });
}

export function listSkillVersions(
  workspaceId: string,
  skillId: string,
  projectId?: string,
): Promise<{ items: SkillVersion[] }> {
  return request(workspaceId, `/${skillId}/versions`, projectId);
}

export function listSkillEvents(
  workspaceId: string,
  skillId: string,
  projectId?: string,
): Promise<{ items: SkillEvent[] }> {
  return request(workspaceId, `/${skillId}/events`, projectId);
}

export function setSkillArchived(
  workspaceId: string,
  skillId: string,
  archived: boolean,
  projectId?: string,
): Promise<SkillDetail> {
  return request(
    workspaceId,
    `/${skillId}/${archived ? "archive" : "restore"}`,
    projectId,
    {
      method: "POST",
      headers: writeHeaders(),
    },
  );
}

export function skillDownloadUrl(
  workspaceId: string,
  skillId: string,
  version: string,
  projectId?: string,
): string {
  const path = `/api/v1/workspaces/${workspaceId}/skills/${skillId}/versions/${encodeURIComponent(version)}/download`;
  return projectQuery(path, projectId);
}
