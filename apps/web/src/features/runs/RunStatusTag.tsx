import { Tag } from "../../components/console";

import { type RunStatus } from "./api";

const labels: Record<RunStatus, string> = {
  QUEUED: "排队中",
  RUNNING: "运行中",
  WAITING_CONTEXT: "等待补充上下文",
  WAITING_APPROVAL: "等待人工审批",
  RECOVERY_REQUIRED: "需要人工恢复",
  SUCCEEDED: "执行成功",
  FAILED: "执行失败",
  CANCELLED: "已取消",
};

export function RunStatusTag({ status }: { status: RunStatus }) {
  const color =
    status === "SUCCEEDED"
      ? "green"
      : status === "FAILED"
        ? "red"
        : status === "RUNNING"
          ? "blue"
          : "grey";
  return <Tag color={color}>{labels[status] ?? status}</Tag>;
}
