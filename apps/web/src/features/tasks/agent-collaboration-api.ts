import { consoleRequest, consoleWrite } from "../runs/api";

const root = "/agent-collaboration";

export type LocalDevice = {
  id: string;
  name: string;
  paired: boolean;
  expires_at: string;
  expired: boolean;
  revoked_at: string | null;
};

export type AgentSession = {
  id: string;
  name: string;
  runtime: string;
  owner_id: string;
  owner_name: string;
  device: string;
  repo: string;
  branch: string;
  online: boolean;
  delivery: "manual" | "queue" | "channel";
};

type OwnedSession = AgentSession & { allowed_members: string[]; task_ids: string[] };
type TaskMail = {
  sessions: AgentSession[];
  messages: {
    id: string;
    sender_id: string;
    recipient_id: string;
    state: string;
    wake_state: string | null;
    body: string;
    created_at: string;
  }[];
  evidence: { id: string; title: string; content: string; session_id: string }[];
};

export const listDevices = (workspace: string) =>
  consoleRequest<{ items: LocalDevice[] }>(workspace, `${root}/devices`);
export const createPairing = (workspace: string, name: string) =>
  consoleWrite<LocalDevice & { pairing_code: string }>(workspace, `${root}/devices`, { name });
export const revokeDevice = (workspace: string, id: string) =>
  consoleWrite<void>(workspace, `${root}/devices/${encodeURIComponent(id)}`, undefined, "DELETE");
export const listMySessions = (workspace: string) =>
  consoleRequest<{ items: OwnedSession[] }>(workspace, `${root}/my-sessions`);
export const listContactMembers = (workspace: string) =>
  consoleRequest<{ items: { id: string; name: string }[] }>(workspace, `${root}/members`);
export const saveContactMembers = (workspace: string, id: string, memberIds: string[]) =>
  consoleWrite<void>(
    workspace, `${root}/my-sessions/${encodeURIComponent(id)}/contacts`,
    { member_ids: memberIds }, "PUT",
  );
export const getTaskAgentMail = (workspace: string, id: string) =>
  consoleRequest<TaskMail>(workspace, `${root}/task/${encodeURIComponent(id)}`);
