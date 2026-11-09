import {
  Banner,
  Button,
  Modal,
  Select,
  TextArea,
  Typography,
} from "../../components/console";
import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { listAgents, listAgentVersions } from "../agents/api";
import { createRun, errorMessage, type RunDetail } from "./api";
import { getProject, listProjects, projectKey } from "../projects/api";

export function CreateRunDialog({
  workspaceId,
  userId,
  open,
  onClose,
  onCreated,
}: {
  workspaceId: string;
  userId: string;
  open: boolean;
  onClose: () => void;
  onCreated: (run: RunDetail) => void;
}) {
  const [agentId, setAgentId] = useState("");
  const [version, setVersion] = useState("");
  const [instruction, setInstruction] = useState("");
  const [projectId, setProjectId] = useState("");
  const [repositoryIds, setRepositoryIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 网络结果不明时重试相同正文沿用同一个键，防止双击或超时产生第二个任务。
  const attempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const agents = useQuery({
    queryKey: ["run-agent-options", workspaceId, userId],
    queryFn: () => listAgents(workspaceId),
    enabled: open,
  });
  const versions = useQuery({
    queryKey: ["run-agent-versions", workspaceId, userId, agentId],
    queryFn: () => listAgentVersions(workspaceId, agentId),
    enabled: open && !!agentId,
  });
  const projects = useQuery({
    queryKey: projectKey(workspaceId, userId),
    queryFn: () => listProjects(workspaceId),
    enabled: open,
  });
  const project = useQuery({
    queryKey: projectKey(workspaceId, userId, projectId),
    queryFn: () => getProject(workspaceId, projectId),
    enabled: open && !!projectId,
    staleTime: 0,
  });
  const available =
    project.data?.repositories.filter((item) => item.status === "active") ?? [];
  const validSelection =
    !projectId ||
    (!project.isError &&
      !project.isPending &&
      project.data?.status === "active" &&
      repositoryIds.length > 0 &&
      repositoryIds.every((id) => available.some((item) => item.id === id)));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (
      submitting ||
      !agentId ||
      !version ||
      !instruction.trim() ||
      !validSelection
    )
      return;
    const input = {
      agent_id: agentId,
      agent_version: version,
      instruction: instruction.trim(),
      ...(projectId
        ? { project_id: projectId, repository_ids: [...repositoryIds].sort() }
        : {}),
    };
    const fingerprint = JSON.stringify(input);
    if (attempt.current?.fingerprint !== fingerprint)
      attempt.current = { fingerprint, key: crypto.randomUUID() };
    setSubmitting(true);
    setError(null);
    try {
      const run = await createRun(workspaceId, input, attempt.current.key);
      attempt.current = null;
      setInstruction("");
      onCreated(run);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      centered
      footer={null}
      title="发起任务"
      visible={open}
      width={680}
      onCancel={() => {
        if (!submitting) onClose();
      }}
      maskClosable={!submitting}
      closeOnEsc={!submitting}
    >
      <form className="agent-form" onSubmit={submit}>
        <Typography.Paragraph type="tertiary">
          选择不可变的已发布版本。本次任务默认仅发起人可见，不隐含推送、合并或部署授权。
        </Typography.Paragraph>
        <div className="form-field">
          <span id="run-project-label">所属项目（可选）</span>
          <Select
            aria-labelledby="run-project-label"
            value={projectId}
            loading={projects.isPending}
            placeholder="不选择项目：普通咨询任务"
            optionList={[
              { value: "", label: "不关联项目" },
              ...(projects.data?.items ?? [])
                .filter((item) => item.status === "active")
                .map((item) => ({ value: item.id, label: item.name })),
            ]}
            onChange={(value) => {
              setProjectId(String(value));
              setRepositoryIds([]);
            }}
          />
        </div>
        {projects.isError && (
          <Banner
            closeIcon={null}
            type="warning"
            description="项目列表加载失败；可关闭后重试，或不关联项目发起普通咨询。"
          />
        )}
        {projectId && (
          <div className="form-field">
            <span id="run-repositories-label">本次涉及的仓库（多选）</span>
            <Select
              aria-labelledby="run-repositories-label"
              multiple
              value={repositoryIds}
              loading={project.isPending}
              disabled={project.isPending || project.isError}
              optionList={available.map((item) => ({
                value: item.id,
                label: `${item.name} · ${item.purpose || item.provider}`,
              }))}
              onChange={(value) => setRepositoryIds(value as string[])}
              placeholder="只选择本次需要的仓库"
            />
            <Typography.Text type="tertiary">
              只使用选中仓库。已启用只读接入时，任务会自动拉取固定提交；只读不代表允许执行命令或推送。
            </Typography.Text>
            {project.isError && (
              <Banner
                closeIcon={null}
                type="danger"
                description="项目或仓库不可用，请重新选择。"
              />
            )}
          </div>
        )}
        <div className="form-field">
          <span id="run-agent-label">已发布 Agent</span>
          <Select
            aria-labelledby="run-agent-label"
            value={agentId || undefined}
            loading={agents.isPending}
            disabled={agents.isPending}
            placeholder="选择 Agent"
            optionList={(agents.data?.items ?? [])
              .filter(
                (item) =>
                  item.status === "active" && item.latest_published_version,
              )
              .map((item) => ({ value: item.id, label: item.display_name }))}
            onChange={(value) => {
              setAgentId(String(value));
              setVersion("");
            }}
          />
        </div>
        <div className="form-field">
          <span id="run-agent-version-label">固定版本</span>
          <Select
            aria-labelledby="run-agent-version-label"
            value={version || undefined}
            disabled={!agentId || versions.isPending}
            loading={!!agentId && versions.isPending}
            placeholder="选择已发布版本"
            optionList={(versions.data?.items ?? []).map((item) => ({
              value: item.version,
              label: item.version,
            }))}
            onChange={(value) => setVersion(String(value))}
          />
        </div>
        {!agents.isPending &&
        !agents.isError &&
        !(agents.data?.items ?? []).some(
          (item) => item.latest_published_version,
        ) ? (
          <Banner
            closeIcon={null}
            description="请先在 Agent 页面发布一个版本，然后再发起任务。"
            type="info"
          />
        ) : null}
        {agents.isError || versions.isError ? (
          <Banner
            closeIcon={null}
            description="Agent 版本加载失败，请关闭弹窗后重试。"
            type="danger"
          />
        ) : null}
        <label className="form-field">
          <span>任务目标</span>
          <TextArea
            required
            maxLength={16000}
            rows={7}
            value={instruction}
            onChange={setInstruction}
            placeholder="描述目标、已知问题与验收条件；不要粘贴密钥。"
          />
        </label>
        {error ? (
          <Banner closeIcon={null} description={error} type="danger" />
        ) : null}
        <div className="modal-actions">
          <Button disabled={submitting} onClick={onClose}>
            取消
          </Button>
          <Button
            htmlType="submit"
            loading={submitting}
            disabled={
              !agentId || !version || !instruction.trim() || !validSelection
            }
            theme="solid"
            type="primary"
          >
            提交任务
          </Button>
        </div>
      </form>
    </Modal>
  );
}
