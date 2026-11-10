import {
  authWrite,
  requestAuth,
  type AuthPrincipal,
  type RegisterInput,
  type Workspace,
} from "./api";

export type WorkspaceMember = {
  id: string;
  display_name: string;
  email: string;
  role: string;
};

export type WorkspaceInvite = {
  id: string;
  creator_name: string;
  created_at: string;
  expires_at: string;
  status: "pending" | "used" | "revoked" | "expired";
  used_by_name: string | null;
};

export type InvitePreview = { workspace_name: string; inviter_name: string };
export type InviteRegistration = Omit<RegisterInput, "workspace_name"> & {
  token: string;
};
export type InviteDuration = 24 | 168;

export const roleName = (role: string) =>
  role === "owner" ? "所有者" : role === "admin" ? "管理员" : "成员";

export function listWorkspaces() {
  return requestAuth<Workspace[]>("/workspaces");
}

export function activateWorkspace(workspaceId: string) {
  return requestAuth<AuthPrincipal>(
    `/workspaces/${workspaceId}/activate`,
    authWrite("POST"),
  );
}

export function listMembers(workspaceId: string) {
  return requestAuth<WorkspaceMember[]>(`/workspaces/${workspaceId}/members`);
}

export function removeMember(workspaceId: string, userId: string) {
  return requestAuth<void>(
    `/workspaces/${workspaceId}/members/${userId}`,
    authWrite("DELETE"),
  );
}

export function listInvitations(workspaceId: string) {
  return requestAuth<WorkspaceInvite[]>(
    `/workspaces/${workspaceId}/invitations`,
  );
}

export function createInvitation(workspaceId: string, hours: InviteDuration) {
  return requestAuth<{ invitation: WorkspaceInvite; token: string }>(
    `/workspaces/${workspaceId}/invitations`,
    authWrite("POST", { expires_in_hours: hours }),
  );
}

export function copyInvitation(workspaceId: string, inviteId: string) {
  return requestAuth<{ token: string }>(
    `/workspaces/${workspaceId}/invitations/${inviteId}/copy`,
    authWrite("POST"),
  );
}

export function revokeInvitation(workspaceId: string, inviteId: string) {
  return requestAuth<void>(
    `/workspaces/${workspaceId}/invitations/${inviteId}`,
    authWrite("DELETE"),
  );
}

export function previewInvitation(token: string, signal?: AbortSignal) {
  return requestAuth<InvitePreview>("/invitations/preview", {
    ...authWrite("POST", { token }),
    ...(signal ? { signal } : {}),
  });
}

export function acceptInvitation(token: string) {
  return requestAuth<AuthPrincipal>(
    "/invitations/accept",
    authWrite("POST", { token }),
  );
}

export function registerFromInvitation(input: InviteRegistration) {
  return requestAuth<AuthPrincipal>(
    "/invitations/register",
    authWrite("POST", input),
  );
}

/** 片段仅留在浏览器。预览和兑换把凭据放在 POST 正文中。 */
export function invitationUrl(token: string) {
  return new URL(`/join#${token}`, window.location.origin).toString();
}
