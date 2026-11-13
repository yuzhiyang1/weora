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

async function request<T>(
  workspaceId: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(
    `/api/v1/workspaces/${workspaceId}/skills${path}`,
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
): Promise<{ items: SkillSummary[]; next_cursor: string | null }> {
  const params = new URLSearchParams();
  if (filters.search) params.set("search", filters.search);
  if (filters.status) params.set("status", filters.status);
  const query = params.size ? `?${params.toString()}` : "";
  return request(workspaceId, query);
}

export function createSkill(
  workspaceId: string,
  file: File,
): Promise<SkillDetail> {
  const form = new FormData();
  form.append("package", file);
  return request(workspaceId, "", {
    method: "POST",
    headers: writeHeaders(),
    body: form,
  });
}

export function getSkill(
  workspaceId: string,
  skillId: string,
): Promise<SkillDetail> {
  return request(workspaceId, `/${skillId}`);
}

/** 源文件作为 JSON 文本返回，浏览器仅在阅读区渲染。 */
export function previewSkillFile(
  workspaceId: string,
  skillId: string,
  source: SkillFileSource,
  path: string,
  signal: AbortSignal,
): Promise<{ text: string | null }> {
  const params = new URLSearchParams({ path });
  if ("version" in source) params.set("version", source.version);
  if ("contributionId" in source) params.set("contribution_id", source.contributionId);
  if ("draft" in source) params.set("draft_sha256", source.draft);
  return request(workspaceId, `/${skillId}/files/preview?${params}`, { signal });
}

/** 仅管理员能查询同名上传的待审核包。 */
export function listSkillContributions(workspaceId: string, skillId: string): Promise<SkillContribution[]> {
  return request(workspaceId, `/${skillId}/contributions`);
}

/** 复用草稿校验报告显示候选包。 */
export function getSkillContribution(workspaceId: string, skillId: string, id: string): Promise<SkillDraft> {
  return request(workspaceId, `/${skillId}/contributions/${id}`);
}

/** 接受时使用服务端修订校验，防止覆盖后来上传的草稿。 */
export function decideSkillContribution(workspaceId: string, skillId: string, id: string, accept: boolean): Promise<void> {
  return request(workspaceId, `/${skillId}/contributions/${id}/decision`, {
    method: "POST",
    headers: writeHeaders("application/json"),
    body: JSON.stringify({ accept }),
  });
}

export function updateSkill(
  workspaceId: string,
  skillId: string,
  input: { display_name: string; description: string },
): Promise<SkillDetail> {
  return request(workspaceId, `/${skillId}`, {
    method: "PATCH",
    headers: writeHeaders("application/json"),
    body: JSON.stringify(input),
  });
}

export function replaceSkillDraft(
  workspaceId: string,
  skillId: string,
  file: File,
): Promise<SkillDraft> {
  const form = new FormData();
  form.append("package", file);
  return request(workspaceId, `/${skillId}/draft`, {
    method: "POST",
    headers: writeHeaders(),
    body: form,
  });
}

export function publishSkill(
  workspaceId: string,
  skillId: string,
  input: { version: string; changelog: string; draft_sha256: string },
): Promise<SkillVersion> {
  return request(workspaceId, `/${skillId}/publish`, {
    method: "POST",
    headers: writeHeaders("application/json"),
    body: JSON.stringify(input),
  });
}

export function listSkillVersions(
  workspaceId: string,
  skillId: string,
): Promise<{ items: SkillVersion[] }> {
  return request(workspaceId, `/${skillId}/versions`);
}

export function listSkillEvents(
  workspaceId: string,
  skillId: string,
): Promise<{ items: SkillEvent[] }> {
  return request(workspaceId, `/${skillId}/events`);
}

export function setSkillArchived(
  workspaceId: string,
  skillId: string,
  archived: boolean,
): Promise<SkillDetail> {
  return request(
    workspaceId,
    `/${skillId}/${archived ? "archive" : "restore"}`,
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
): string {
  return `/api/v1/workspaces/${workspaceId}/skills/${skillId}/versions/${encodeURIComponent(version)}/download`;
}
