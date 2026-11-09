import { useId, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { buttonVariants } from "@heroui/styles";

import {
  Banner,
  Button,
  Card,
  Select,
  Spin,
  Tag,
  Typography,
} from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { errorMessage } from "../runs/api";
import { RunStatusTag } from "../runs/RunStatusTag";
import {
  setTaskStatus,
  getTask,
  listTasks,
  tasksKey,
  type TaskDetail,
  type TaskStatus,
} from "./api";
import { AssignTaskDialog, ReviewTaskDialog, ShareTaskDialog } from "./TaskDialogs";
import { TaskBoard } from "./TaskBoard";
import { taskStatusOptions, taskStatuses } from "./task-status";
import "./tasks.css";

export function TasksPage() {
  const { principal } = useAuth();
  const workspaceId = principal?.workspace.id ?? "";
  const query = useQuery({
    queryKey: tasksKey(workspaceId),
    queryFn: () => listTasks(workspaceId),
    enabled: !!workspaceId,
  });
  return (
    <section className="detail-page">
      {query.isPending ? (
        <Spin tip="正在读取协作任务" />
      ) : query.isError ? (
        <Banner
          type="danger"
          title="协作任务暂时无法加载"
          description={
            <div>
              <p>{errorMessage(query.error)}</p>
              <Button size="small" onClick={() => void query.refetch()}>
                重新加载
              </Button>
            </div>
          }
        />
      ) : (
        <TaskBoard
          tasks={query.data.items}
          memberId={principal?.user.id ?? ""}
        />
      )}
    </section>
  );
}

function activityText(item: TaskDetail["activity"][number]) {
  switch (item.action) {
    case "shared":
      return "更新共享设置";
    case "accepted":
      return "负责人已接手";
    case "review_submitted":
      return "提交验收";
    case "review_accepted":
      return "验收通过";
    case "review_returned":
      return "退回修改";
    case "status_changed":
      return `状态改为 ${taskStatuses[item.note as TaskStatus]?.label ?? item.note}`;
    case "started":
      return `已分配给 ${item.target} 并开始运行`;
    case "handoff_wait":
      return `等待本轮结束后交接给 ${item.target}`;
    case "handoff_stop":
      return `请求停止本轮后交接给 ${item.target}`;
    case "handoff_expired":
      return `交接已失效 · ${item.target}`;
    default:
      if (!item.target) return "移除负责人";
      return item.previous && item.previous !== item.target
        ? `负责人由 ${item.previous} 变更为 ${item.target}`
        : `已指派给 ${item.target}`;
  }
}

export function TaskDetailPage() {
  const { taskId } = useParams();
  return <TaskDetailContent key={taskId} taskId={taskId ?? ""} />;
}

function TaskSection({ title, className, embedded, children }: {
  title: string;
  className: string;
  embedded: boolean;
  children: ReactNode;
}) {
  if (!embedded) return <Card title={title} className={className}>{children}</Card>;
  return (
    <section className={className}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export function TaskDetailContent({ taskId, embedded = false }: { taskId: string; embedded?: boolean }) {
  const { principal } = useAuth();
  const workspaceId = principal?.workspace.id ?? "";
  const statusLabelId = useId();
  const client = useQueryClient();
  const [dialog, setDialog] = useState<{
    kind: "share" | "assign" | "submit" | "return";
    task: TaskDetail;
  } | null>(null);
  const queryKey = [...tasksKey(workspaceId), taskId];
  const taskQuery = useQuery({
    queryKey,
    queryFn: () => getTask(workspaceId, taskId),
    enabled: !!workspaceId && !!taskId,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.active_run || query.state.data?.pending_assignment
        ? 3000
        : false,
  });
  const [started, setStarted] = useState<string>();
  function saved(runId?: string) {
    setStarted(runId);
    setDialog(null);
    void client.invalidateQueries({ queryKey: tasksKey(workspaceId) });
    void client.invalidateQueries({ queryKey: ["inbox", workspaceId] });
  }
  const statusChange = useMutation({
    mutationFn: ({
      revision,
      status,
    }: {
      revision: number;
      status: TaskStatus;
    }) => setTaskStatus(workspaceId, taskId, revision, status),
    onSuccess: () => saved(),
  });
  if (taskQuery.isPending) return <Spin tip="正在读取任务" />;
  if (taskQuery.isError)
    return <Banner type="danger" description={errorMessage(taskQuery.error)} />;
  const task = taskQuery.data;
  const inReview = task.status === "in_review";
  const canSubmitReview = task.can_assign && task.status !== "done" && task.status !== "cancelled";
  const deliveryNote = task.activity.find((item) => item.action === "review_submitted")?.note;
  const accessibleRun = task.active_run?.id ?? started;
  return (
    <section className={embedded ? "task-detail task-detail-embedded" : "detail-page task-detail"}>
      {!embedded && (
        <Link className={buttonVariants({ variant: "outline", size: "sm" })} to="/tasks">
          返回协作任务
        </Link>
      )}
      <header className="page-header">
        <div>
          <Typography.Title heading={1}>
            {task.title ?? "任务协作"}
          </Typography.Title>
          <div className="task-status">
            <Tag color={taskStatuses[task.status].color}>
              {taskStatuses[task.status].label}
            </Tag>
            {task.active_run && (
              <RunStatusTag status={task.active_run.status} />
            )}
            <span className="task-muted">
              {task.title ? "指定成员可见" : "仅发起人可见"}
            </span>
          </div>
        </div>
      </header>
      {task.pending_assignment && (
        <Banner
          type="warning"
          title={`待交接给 ${task.pending_assignment.name}`}
          description={
            task.active_run?.stopping
              ? "已请求停止当前运行，确认结束后完成交接。"
              : "当前运行结束后完成交接。"
          }
        />
      )}
      {statusChange.isError && (
        <Banner type="danger" description={errorMessage(statusChange.error)} />
      )}
      <div className="task-columns">
        <TaskSection title="任务目标" className="task-goal" embedded={embedded}>
          <p className="task-summary">
            {task.summary ?? "发布协作标题和摘要后，可以邀请成员并指派负责人。"}
          </p>
          {accessibleRun && (
            <Link className="task-link" to={`/runs/${accessibleRun}`}>
              查看运行详情
            </Link>
          )}
        </TaskSection>
        <TaskSection title="协作属性" className="task-attribute-section" embedded={embedded}>
          <dl className="task-properties">
            <dt id={statusLabelId}>状态</dt>
            <dd>
              {task.can_assign && task.title && !inReview ? (
                <Select
                  aria-labelledby={statusLabelId}
                  optionList={taskStatusOptions.filter((option) => option.value !== "in_review")}
                  value={task.status}
                  disabled={statusChange.isPending}
                  onChange={(value) => {
                    if (
                      typeof value === "string" &&
                      Object.hasOwn(taskStatuses, value) &&
                      value !== task.status
                    ) {
                      statusChange.mutate({
                        revision: task.revision,
                        status: value as TaskStatus,
                      });
                    }
                  }}
                />
              ) : (
                taskStatuses[task.status].label
              )}
            </dd>
            <dt>负责人</dt>
            <dd>{task.assignment?.name ?? "未分配"}</dd>
            <dt>参与人</dt>
            <dd>
              {task.participants.map((person) => person.name).join("、") ||
                "暂无"}
            </dd>
          </dl>
          <div className="task-actions task-properties-actions">
            {task.can_assign && task.title && !inReview && (
              <Button
                disabled={!!task.pending_assignment}
                onClick={() => setDialog({ kind: "assign", task })}
              >
                {task.assignment ? "更换负责人" : "指派负责人"}
              </Button>
            )}
            {task.can_share && (
              <Button
                type="primary"
                theme="solid"
                onClick={() => setDialog({ kind: "share", task })}
              >
                共享
              </Button>
            )}
          </div>
        </TaskSection>
      </div>
      {task.title && (inReview || canSubmitReview) && (
        <TaskSection title="任务验收" className="task-history task-review-section" embedded={embedded}>
          {inReview ? (
            <>
              <p className="task-summary">{deliveryNote ?? "任务已进入审核，等待发起人验收。"}</p>
              {task.can_share && (
                <div className="task-actions task-properties-actions">
                  <Button
                    type="primary"
                    theme="solid"
                    loading={statusChange.isPending}
                    onClick={() => statusChange.mutate({ revision: task.revision, status: "done" })}
                  >
                    通过验收
                  </Button>
                  <Button disabled={statusChange.isPending} onClick={() => setDialog({ kind: "return", task })}>
                    退回修改
                  </Button>
                </div>
              )}
            </>
          ) : (
            <>
              <p className="task-muted">
                {task.active_run || task.pending_assignment ? "本轮运行和交接结束后可提交验收。" : "提交交付说明，由任务发起人验收。"}
              </p>
              <Button
                disabled={!!task.active_run || !!task.pending_assignment || statusChange.isPending}
                onClick={() => setDialog({ kind: "submit", task })}
              >
                提交验收
              </Button>
            </>
          )}
        </TaskSection>
      )}
      <TaskSection title="任务动态" className="task-history task-activity-section" embedded={embedded}>
        {task.activity.length ? (
          <ol className="task-activity">
            {task.activity.map((item) => (
              <li key={item.revision}>
                <strong>
                  {item.actor_name && `${item.actor_name} · `}
                  {activityText(item)}
                </strong>
                <time dateTime={item.at}>
                  {new Date(item.at).toLocaleString("zh-CN")}
                </time>
                {item.note && item.action !== "status_changed" && (
                  <p className="task-summary">{item.note}</p>
                )}
              </li>
            ))}
          </ol>
        ) : (
          <p className="task-muted">暂无协作记录。</p>
        )}
      </TaskSection>
      {dialog?.kind === "share" && (
        <ShareTaskDialog
          workspaceId={workspaceId}
          task={dialog.task}
          onClose={() => setDialog(null)}
          onSaved={saved}
        />
      )}
      {dialog?.kind === "assign" && (
        <AssignTaskDialog
          workspaceId={workspaceId}
          task={dialog.task}
          onClose={() => setDialog(null)}
          onSaved={saved}
        />
      )}
      {(dialog?.kind === "submit" || dialog?.kind === "return") && (
        <ReviewTaskDialog
          kind={dialog.kind}
          workspaceId={workspaceId}
          task={dialog.task}
          onClose={() => setDialog(null)}
          onSaved={saved}
        />
      )}
    </section>
  );
}
