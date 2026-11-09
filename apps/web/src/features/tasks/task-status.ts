import {
  Ban,
  Circle,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleX,
  Clock,
} from "lucide-react";
import type { TaskStatus } from "./api";

// 状态沿用 Multica 的内置键，Run 进度单独显示。
export const taskStatuses = {
  backlog: { label: "待规划", icon: CircleDashed, color: "grey" },
  todo: { label: "待办", icon: Circle, color: "grey" },
  in_progress: { label: "进行中", icon: CircleDot, color: "orange" },
  in_review: { label: "审核中", icon: Clock, color: "green" },
  blocked: { label: "已阻塞", icon: Ban, color: "red" },
  done: { label: "已完成", icon: CircleCheck, color: "blue" },
  cancelled: { label: "已取消", icon: CircleX, color: "grey" },
} satisfies Record<
  TaskStatus,
  { label: string; icon: typeof Circle; color: string }
>;

export const taskStatusOptions = (
  Object.keys(taskStatuses) as TaskStatus[]
).map((value) => ({
  value,
  label: taskStatuses[value].label,
}));
