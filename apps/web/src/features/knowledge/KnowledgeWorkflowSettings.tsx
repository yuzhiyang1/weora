import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Banner, Button, Card, Input, Modal, Skeleton } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { errorMessage } from "../runs/api";
import { saveWorkflowDefinition, workflowDefinition, workflowNames, type Scope, type WorkflowName } from "./api";
import { KnowledgeRows } from "./KnowledgeRows";

const labels = { "teamai-share-learnings": "会话经验分享", "teamai-share-session": "会话归档与评分" };
const policies = { allow: "允许设备发起", ask: "需要本人确认", deny: "仅本人可发起" };
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const maxWorkflowYamlBytes = 128 * 1024;
const invalidApprovalLink = "请输入有效的审批链接或工作流运行 ID。";
const queryKey = (workspace: string, user: string, name: WorkflowName) =>
  ["knowledge-definition", workspace, user, name];

type Edit = { name: WorkflowName; revision: number; definition: string | null; file?: string };

export function KnowledgeWorkflowSettings({ scope, user, reviewer }: { scope: Scope; user: string; reviewer: boolean }) {
  const cache = useQueryClient();
  const [name, setName] = useState<WorkflowName>(workflowNames[0]);
  const [edit, setEdit] = useState<Edit>();
  const [error, setError] = useState("");
  const upload = useRef<HTMLInputElement>(null);
  const results = useQueries({ queries: workflowNames.map((key) => ({
    queryKey: queryKey(scope.workspace, user, key),
    queryFn: () => workflowDefinition(scope.workspace, key), enabled: reviewer,
  })) });
  const selected = results[workflowNames.indexOf(name)]?.data;
  const save = useMutation({
    mutationFn: (pending: Edit) => saveWorkflowDefinition(scope.workspace, pending.name, pending.revision, pending.definition),
    onSuccess: (updated, pending) => {
      cache.setQueryData(queryKey(scope.workspace, user, pending.name), updated);
      setEdit(undefined);
    },
  });
  async function previewWorkflowUpload(file: File) {
    setError("");
    if (!selected) return;
    if (file.size > maxWorkflowYamlBytes) {
      setError("工作流文件不能超过 128 KiB。");
      return;
    }

    let content: string;
    try {
      content = await file.text();
    } catch {
      setError("工作流文件暂时无法读取。");
      return;
    }
    if (!content.trim()) {
      setError("工作流 YAML 不能为空。");
      return;
    }

    // 确认时沿用选文件那一刻的定义版本，让服务端发现期间发生的覆盖。
    setEdit({ file: file.name, name, revision: selected.revision, definition: content });
  }

  const failed = results.find((result) => result.isError);
  return <div className="knowledge-stack">
    {reviewer && (failed ? <AssetLoadError title="团队工作流暂时无法加载" description={errorMessage(failed.error)}
      onRetry={() => void Promise.all(results.map((result) => result.refetch()))} />
      : results.some((result) => result.isPending) ? <Skeleton active /> :
      <>
        <KnowledgeRows title="团队工作流" empty="没有可管理的工作流"
          rows={workflowNames.flatMap((key, index) => {
            const item = results[index]?.data;
            return item ? [{
              id: key, title: labels[key],
              meta: `${policies[item.definition.invoke]} · ${item.overridden ? `定义版本 ${item.revision}` : "默认定义"}`,
              onOpen: () => { setError(""); setName(key); },
            }] : [];
          })} />
        {selected && <Card title="团队工作流定义">
          <div className="knowledge-stack">
            <p className="knowledge-text"><strong>{labels[name]}</strong> · 工作空间范围
              {selected.definition.alias && ` · ${selected.definition.alias}`}</p>
            <p className="knowledge-meta">上传 YAML 覆盖。正在运行的流程继续使用已固定的版本。</p>
            {error && <Banner closeIcon={null} type="danger" description={error} />}
            <div className="knowledge-inline">
              <Button disabled={!selected.overridden} onClick={() => {
                save.reset();
                setEdit({ name, revision: selected.revision, definition: null });
              }}>恢复默认</Button>
              <Button theme="solid" onClick={() => { save.reset(); upload.current?.click(); }}>上传覆盖</Button>
              <input ref={upload} type="file" accept=".yaml,.yml" hidden onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                // 清空文件控件，取消预览后仍可重新选择同一个文件。
                event.currentTarget.value = "";
                if (file) void previewWorkflowUpload(file);
              }} />
            </div>
          </div>
        </Card>}
      </>)}
    <ApprovalLink scope={scope} />
    {edit && <Modal title={edit.file ? "确认覆盖工作流" : "恢复默认工作流"} visible
      onCancel={() => setEdit(undefined)} maskClosable={!save.isPending} closeOnEsc={!save.isPending}>
      <div className="knowledge-form">
        <p className="knowledge-text">{labels[edit.name]} · 基准版本 {edit.revision}</p>
        <p className="knowledge-meta">{edit.file ? `上传文件 · ${edit.file}` : "新发起的流程将使用内置定义。"}</p>
        {save.isError && <Banner closeIcon={null} type="danger" description={errorMessage(save.error)} />}
        <div className="knowledge-actions">
          <Button onClick={() => setEdit(undefined)} disabled={save.isPending}>取消</Button>
          <Button theme="solid" loading={save.isPending} onClick={() => save.mutate(edit)}>
            {edit.file ? "保存覆盖" : "确认恢复"}
          </Button>
        </div>
      </div>
    </Modal>}
  </div>;
}

function ApprovalLink({ scope }: { scope: Scope }) {
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  function openApproval() {
    let runId = input.trim();
    let project = scope.project;

    // 裸 ID 使用当前范围。CLI 链接携带发起时的范围，打开前先核对工作空间。
    if (!uuid.test(runId)) {
      let link: URL;
      try {
        link = new URL(runId, location.origin);
      } catch {
        setError(invalidApprovalLink);
        return;
      }
      const linkedWorkspace = link.searchParams.get("workspace");
      if (linkedWorkspace && linkedWorkspace !== scope.workspace) {
        setError("请先切换到审批链接所属的工作空间。");
        return;
      }
      runId = /^\/knowledge\/workflows\/([^/]+)$/.exec(link.pathname)?.[1] ?? "";
      project = link.searchParams.get("project") ?? "";
    }
    if (!uuid.test(runId) || (project && !uuid.test(project))) {
      setError(invalidApprovalLink);
      return;
    }

    const params = new URLSearchParams({ workspace: scope.workspace });
    if (project) params.set("project", project);
    navigate(`/knowledge/workflows/${runId}?${params}`);
  }
  return <Card title="打开审批">
    <form className="knowledge-stack knowledge-approval-link" onSubmit={(event) => {
      event.preventDefault();
      openApproval();
    }}>
      <Input aria-label="审批链接或运行 ID" placeholder="粘贴审批链接或输入运行 ID" value={input}
        onChange={(value) => { setInput(value); setError(""); }} />
      {error && <Banner closeIcon={null} type="danger" description={error} />}
      <div><Button htmlType="submit" theme="solid" disabled={!input.trim()}>打开审批</Button></div>
    </form>
  </Card>;
}
