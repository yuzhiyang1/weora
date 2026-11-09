import { consoleRequest, consoleWrite } from "../runs/api";

export type Connection = {
  id: string;
  name: string;
  app_id: string;
  enabled: boolean;
  connection_status: string;
  event_status: string;
};
export type Source = {
  id: string;
  connection_id: string;
  chat_id: string;
  name: string;
  enabled: boolean;
  history_status: string;
  bot_membership: string;
};
export type Grant = { source_id: string; user_id: string; enabled: boolean };
export type Binding = {
  connection_id: string;
  open_id: string;
  user_id: string;
  enabled: boolean;
};
export type Collection = { id: string; name: string; source_ids: string[] };
export type ContextResource = {
  type: "image" | "file" | "audio" | "media" | "sticker" | "folder";
  image_key?: string;
  file_key?: string;
  file_name?: string;
  duration?: number;
  downloaded: false;
  parsed: false;
};
export type ContextMessage = {
  ref: string;
  message_id: string;
  thread_id: string | null;
  author_id: string;
  created_at: string;
  text: string;
  content_type: string;
  // 老快照没有解析字段，界面不得据此推断附件或富文本已经完整读取。
  parse_status?: "complete" | "partial" | "unsupported" | "recalled";
  parse_notes?: string[];
  resources?: ContextResource[];
};
export type Snapshot = {
  snapshot_id: string;
  source_id?: string;
  status: "complete" | "partial" | "unavailable";
  messages?: ContextMessage[];
  window?: { start_time: string; end_time: string };
  fetched_at?: string;
  expires_at?: string;
  next_cursor?: string | null;
  expired?: boolean;
  coverage?: {
    window_complete: boolean;
    truncated: boolean;
    reason: string | null;
  };
  query?: {
    thread_id: string | null;
    message_id: string | null;
    limit: number;
  };
  error?: { code: string; message: string } | null;
};
export type RunChannelStatus = {
  inbox: {
    id: string;
    message_id: string;
    status: string;
    ingress: "manual_history" | "feishu_event" | "compensation";
    addressed?: boolean;
    coordination_scope_id?: string | null;
  } | null;
  deliveries: {
    id: string;
    status: string;
    attempts: number;
    remote_message_id: string | null;
    business_key: string;
  }[];
};
export const getRunChannelStatus = (workspaceId: string, runId: string) =>
  consoleRequest<RunChannelStatus>(
    workspaceId,
    `/context/runs/${encodeURIComponent(runId)}/channel-status`,
  );

export const contextList = <T>(workspaceId: string, path: string) =>
  consoleRequest<{ items: T[] }>(workspaceId, `/context/${path}`);
export const contextSave = <T>(
  workspaceId: string,
  path: string,
  body: unknown,
  method = "POST",
) => consoleWrite<T>(workspaceId, `/context/${path}`, body, method);
export const listSnapshots = (workspaceId: string, runId: string) =>
  contextList<Snapshot>(workspaceId, `runs/${runId}/snapshots`);
export const previewSnapshot = (
  workspaceId: string,
  runId: string,
  snapshotId: string,
) =>
  consoleRequest<Snapshot>(
    workspaceId,
    `/context/runs/${runId}/snapshots/${snapshotId}`,
  );
export type ContextReadInput = {
  source_id: string;
  start_time: string;
  end_time: string;
  limit: number;
  cursor?: string;
  thread_id?: string;
  message_id?: string;
};
export const readContext = (
  workspaceId: string,
  runId: string,
  input: ContextReadInput,
) => contextSave<Snapshot>(workspaceId, `runs/${runId}/messages`, input);
