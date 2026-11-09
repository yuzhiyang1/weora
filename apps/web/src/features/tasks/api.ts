import { consoleRequest, consoleWrite, type RunStatus } from "../runs/api";

export type Assignment = {
  kind: "member" | "agent";
  id: string;
  name: string;
  version: string | null;
};

export type TaskStatus =
  | "backlog"
  | "todo"
  | "in_progress"
  | "in_review"
  | "blocked"
  | "done"
  | "cancelled";

export type TaskSummary = {
  id: string;
  title: string | null;
  summary: string | null;
  participant_ids: string[];
  assignment: Assignment | null;
  pending_assignment: Assignment | null;
  status: TaskStatus;
  revision: number;
  can_share: boolean;
  can_assign: boolean;
};

export type TaskDetail = TaskSummary & {
  participants: MemberOption[];
  active_run: {
    status: RunStatus;
    id: string | null;
    stopping: boolean;
  } | null;
  activity: {
    revision: number;
    action: string;
    actor_name: string | null;
    note: string;
    previous: string | null;
    target: string | null;
    at: string;
  }[];
};

export type MemberOption = { id: string; name: string };
export type AssignmentMode = "assign" | "start";
export type AssignmentInput = {
  revision: number;
  kind: Assignment["kind"] | null;
  assignee_id: string | null;
  version: string | null;
  mode: AssignmentMode;
  stop_current: boolean;
};

export const tasksKey = (workspaceId: string) =>
  ["tasks", workspaceId] as const;

// 协作接口单独读取已发布内容，运行正文仍由现有 Run API 鉴权。
const taskPath = (taskId: string) => `/tasks/${encodeURIComponent(taskId)}`;

export function listTasks(workspaceId: string) {
  return consoleRequest<{ items: TaskSummary[] }>(workspaceId, "/tasks");
}

export function getTask(workspaceId: string, taskId: string) {
  return consoleRequest<TaskDetail>(workspaceId, taskPath(taskId));
}

export function taskMembers(workspaceId: string, taskId: string) {
  return consoleRequest<{ items: MemberOption[] }>(
    workspaceId,
    `${taskPath(taskId)}/members`,
  );
}

export function shareTask(
  workspaceId: string,
  taskId: string,
  input: {
    revision: number;
    title: string;
    summary: string;
    participant_ids: string[];
  },
) {
  return consoleWrite<void>(
    workspaceId,
    `${taskPath(taskId)}/sharing`,
    input,
    "PUT",
  );
}

export function assignTask(
  workspaceId: string,
  taskId: string,
  input: AssignmentInput,
  key: string,
) {
  return consoleWrite<{ run_id: string | null }>(
    workspaceId,
    `${taskPath(taskId)}/assignment`,
    input,
    "POST",
    { "Idempotency-Key": key },
  );
}

export function setTaskStatus(
  workspaceId: string,
  taskId: string,
  revision: number,
  status: TaskStatus,
  note?: string,
) {
  return consoleWrite<void>(
    workspaceId,
    `${taskPath(taskId)}/status`,
    { revision, status, note },
    "PATCH",
  );
}
