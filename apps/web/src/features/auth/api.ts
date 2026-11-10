export type AuthPrincipal = {
  user: {
    id: string;
    display_name: string;
    email: string;
    avatar_url?: string | null;
  };
  workspace: Workspace;
};

export type Workspace = { id: string; name: string; role: string };

export type LoginInput = {
  email: string;
  password: string;
  invite_token?: string;
};

export type RegisterInput = Omit<LoginInput, "invite_token"> & {
  display_name: string;
  workspace_name: string;
};

type ErrorBody = {
  detail?:
    | {
        code?: string;
        message?: string;
      }
    | Array<{
        loc?: Array<string | number>;
        msg?: string;
        type?: string;
      }>;
};

export class AuthApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "AuthApiError";
  }
}

export async function requestAuth<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`/api/v1/auth${path}`, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  });

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    const detail = body.detail;
    const isValidationError = Array.isArray(detail);
    throw new AuthApiError(
      isValidationError
        ? "请检查表单内容后重试"
        : (detail?.message ?? "请求未完成，请稍后再试"),
      response.status,
      isValidationError ? undefined : detail?.code,
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function getCurrentPrincipal(): Promise<AuthPrincipal> {
  return requestAuth<AuthPrincipal>("/me");
}

export function authWrite(method: string, body?: unknown): RequestInit {
  const csrfToken = readCookie("weora_csrf");
  return {
    method,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(csrfToken ? { "X-CSRF-Token": decodeURIComponent(csrfToken) } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  };
}

export function saveProfile(
  display_name: string,
  avatar?: string | null,
): Promise<AuthPrincipal> {
  return requestAuth<AuthPrincipal>(
    "/profile",
    authWrite("PUT", {
      display_name,
      ...(avatar !== undefined ? { avatar } : {}),
    }),
  );
}

export function login(input: LoginInput): Promise<AuthPrincipal> {
  return requestAuth<AuthPrincipal>("/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function register(input: RegisterInput): Promise<AuthPrincipal> {
  return requestAuth<AuthPrincipal>("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

function readCookie(name: string): string | undefined {
  const prefix = `${encodeURIComponent(name)}=`;
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
    ?.slice(prefix.length);
}

export function logout(): Promise<void> {
  return requestAuth<void>("/logout", authWrite("POST"));
}
