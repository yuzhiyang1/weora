import { useId, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import {
  Banner,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  TextArea,
} from "../../components/console";
import { listAgents } from "../agents/api";
import { errorMessage } from "../runs/api";
import {
  assignTask,
  setTaskStatus,
  shareTask,
  taskMembers,
  type AssignmentMode,
  type TaskDetail,
} from "./api";

type DialogProps = {
  workspaceId: string;
  task: TaskDetail;
  onClose: () => void;
  onSaved: (runId?: string) => void;
};

export function ReviewTaskDialog({
  workspaceId,
  task,
  kind,
  onClose,
  onSaved,
}: DialogProps & { kind: "submit" | "return" }) {
  const noteId = useId();
  const [note, setNote] = useState("");
  const submit = kind === "submit";
  const title = submit ? "提交验收" : "退回修改";
  const save = useMutation({
    mutationFn: () =>
      setTaskStatus(workspaceId, task.id, task.revision, submit ? "in_review" : "in_progress", note.trim()),
    onSuccess: () => onSaved(),
  });
  return (
    <Modal
      title={title}
      visible
      onCancel={onClose}
      width={540}
      closeOnEsc={!save.isPending}
      maskClosable={!save.isPending}
    >
      <form
        className="task-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (note.trim() && !save.isPending) save.mutate();
        }}
      >
        <label htmlFor={noteId}>{submit ? "交付说明" : "退回原因"}</label>
        <TextArea id={noteId} value={note} onChange={setNote} required maxLength={4000} rows={4} />
        {save.isError && <Banner type="danger" description={errorMessage(save.error)} />}
        <div className="task-actions">
          <Button disabled={save.isPending} onClick={onClose}>取消</Button>
          <Button
            htmlType="submit"
            type="primary"
            theme="solid"
            loading={save.isPending}
            disabled={!note.trim()}
          >
            {title}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function ShareTaskDialog({
  workspaceId,
  task,
  onClose,
  onSaved,
}: DialogProps) {
  const titleId = useId();
  const summaryId = useId();
  const membersId = useId();
  const [title, setTitle] = useState(task.title ?? "");
  const [summary, setSummary] = useState(task.summary ?? "");
  const [participants, setParticipants] = useState(task.participant_ids);
  const members = useQuery({
    queryKey: ["task-members", workspaceId, task.id],
    queryFn: () => taskMembers(workspaceId, task.id),
  });
  const save = useMutation({
    mutationFn: () =>
      shareTask(workspaceId, task.id, {
        revision: task.revision,
        title,
        summary,
        participant_ids: participants,
      }),
    onSuccess: () => onSaved(),
  });
  const lockedIds = [task.assignment, task.pending_assignment]
    .filter((item) => item?.kind === "member")
    .map((item) => item?.id);
  return (
    <Modal
      title="共享任务"
      visible
      onCancel={onClose}
      width={540}
      closeOnEsc={!save.isPending}
      maskClosable={!save.isPending}
    >
      <form
        className="task-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!save.isPending) save.mutate();
        }}
      >
        <p className="task-muted">
          指定成员可查看协作摘要。原始材料按各自权限访问。
        </p>
        <label htmlFor={titleId}>协作标题</label>
        <Input
          id={titleId}
          value={title}
          onChange={setTitle}
          required
          maxLength={160}
        />
        <label htmlFor={summaryId}>协作摘要</label>
        <TextArea
          id={summaryId}
          value={summary}
          onChange={setSummary}
          required
          maxLength={8000}
          rows={4}
        />
        <span id={membersId}>参与成员</span>
        <Select
          aria-labelledby={membersId}
          multiple
          placeholder="搜索工作空间成员"
          value={participants}
          loading={members.isPending}
          disabled={members.isError || save.isPending}
          optionList={(members.data?.items ?? []).map((member) => ({
            value: member.id,
            label: member.name,
            disabled: lockedIds.includes(member.id),
          }))}
          onChange={(values) =>
            setParticipants(Array.isArray(values) ? values.map(String) : [])
          }
        />
        {lockedIds.length > 0 && (
          <small className="task-muted">当前负责人需要保留访问权限。</small>
        )}
        {members.isError && (
          <Banner type="danger" description={errorMessage(members.error)} />
        )}
        {save.isError && (
          <Banner type="danger" description={errorMessage(save.error)} />
        )}
        <div className="task-actions">
          <Button disabled={save.isPending} onClick={onClose}>
            取消
          </Button>
          <Button
            htmlType="submit"
            type="primary"
            theme="solid"
            loading={save.isPending}
            disabled={!title.trim() || !summary.trim() || !members.isSuccess}
          >
            保存共享
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function AssignTaskDialog({
  workspaceId,
  task,
  onClose,
  onSaved,
}: DialogProps) {
  const choiceId = useId();
  const hasActiveRun = task.active_run !== null;
  const [selection, setSelection] = useState(
    task.assignment ? `${task.assignment.kind}:${task.assignment.id}` : "",
  );
  const [stopCurrent, setStopCurrent] = useState(false);
  const submitted = useRef<{ fingerprint: string; key: string } | null>(null);
  const members = useQuery({
    queryKey: ["task-members", workspaceId, task.id],
    queryFn: () => taskMembers(workspaceId, task.id),
  });
  const agents = useQuery({
    queryKey: ["task-agents", workspaceId],
    queryFn: () => listAgents(workspaceId),
  });
  const choices = [
    ...(members.data?.items ?? []).map((member) => ({
      id: member.id,
      key: `member:${member.id}`,
      kind: "member" as const,
      name: `成员 · ${member.name}`,
      version: null,
    })),
    ...(agents.data?.items ?? [])
      .filter(
        (agent) => agent.status === "active" && agent.latest_published_version,
      )
      .map((agent) => ({
        id: agent.id,
        key: `agent:${agent.id}`,
        kind: "agent" as const,
        name: `Agent · ${agent.display_name} · v${agent.latest_published_version}`,
        version: agent.latest_published_version,
      })),
  ];
  const selected = choices.find((item) => item.key === selection);
  const removing = selection === "unassigned" && !!task.assignment;
  const save = useMutation({
    mutationFn: (mode: AssignmentMode) => {
      if (!selected && !removing) throw new Error("请选择负责人");
      const input = {
        revision: task.revision,
        kind: selected?.kind ?? null,
        assignee_id: selected?.id ?? null,
        version: selected?.version ?? null,
        mode,
        stop_current: stopCurrent,
      };
      const fingerprint = JSON.stringify(input);
      // 同一表单的网络重试使用相同请求键；变更操作后生成新键。
      if (submitted.current?.fingerprint !== fingerprint) {
        submitted.current = { fingerprint, key: crypto.randomUUID() };
      }
      return assignTask(workspaceId, task.id, input, submitted.current.key);
    },
    onSuccess: ({ run_id }) => onSaved(run_id ?? undefined),
  });
  return (
    <Modal
      title={task.assignment || hasActiveRun ? "更换负责人" : "指派负责人"}
      visible
      onCancel={onClose}
      width={540}
      closeOnEsc={!save.isPending}
      maskClosable={!save.isPending}
    >
      <div className="task-form">
        <span id={choiceId}>负责人</span>
        <Select
          aria-labelledby={choiceId}
          placeholder="搜索成员或 Agent"
          value={selection}
          loading={members.isPending || agents.isPending}
          disabled={save.isPending}
          optionList={[
            ...(task.assignment
              ? [{ value: "unassigned", label: "未分配" }]
              : []),
            ...choices.map((item) => ({ value: item.key, label: item.name })),
          ]}
          onChange={(value) => setSelection(String(value))}
        />
        {hasActiveRun && (
          <>
            <Checkbox
              checked={stopCurrent}
              disabled={save.isPending}
              onChange={(event) => setStopCurrent(event.target.checked)}
            >
              停止上一轮运行
            </Checkbox>
            <p className="task-muted">
              保存后立即更换负责人。上一轮仍活跃时，新运行需等待。
            </p>
          </>
        )}
        {!hasActiveRun && selected?.kind === "agent" && (
          <p className="task-muted">
            {task.status === "backlog"
              ? "待规划任务移到待办后可启动。"
              : "可以先保存负责人，或立即启动一次运行。"}
          </p>
        )}
        {(members.isError || agents.isError) && (
          <Banner
            type="warning"
            description="部分负责人选项读取失败，可重开弹窗重试。"
          />
        )}
        {save.isError && (
          <Banner type="danger" description={errorMessage(save.error)} />
        )}
        <div className="task-actions">
          <Button disabled={save.isPending} onClick={onClose}>
            取消
          </Button>
          {!hasActiveRun &&
          selected?.kind === "agent" &&
          task.status !== "backlog" ? (
            <>
              <Button
                disabled={save.isPending}
                onClick={() => save.mutate("assign")}
              >
                暂不开始
              </Button>
              <Button
                type="primary"
                theme="solid"
                loading={save.isPending}
                onClick={() => save.mutate("start")}
              >
                分配并开始
              </Button>
            </>
          ) : (
            <Button
              type="primary"
              theme="solid"
              loading={save.isPending}
              disabled={!selected && !removing}
              onClick={() => save.mutate("assign")}
            >
              {removing ? "移除负责人" : "确认指派"}
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
