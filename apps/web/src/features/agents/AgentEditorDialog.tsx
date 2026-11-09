import { Puzzle as IconPuzzle } from "lucide-react";
import {
  Banner,
  Button,
  Checkbox,
  Input,
  Modal,
  Spin,
  Tag,
  TextArea,
  Typography,
} from "../../components/console";
import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import {
  listSkills,
  listSkillVersions,
  type SkillSummary,
  type SkillVersion,
} from "../skills/api";
import { AgentsApiError, type CreateAgentInput } from "./api";

type SkillOption = { skill: SkillSummary; version: SkillVersion };

type AgentEditorDialogProps = {
  workspaceId: string;
  open: boolean;
  onClose: () => void;
  onSubmit: (input: CreateAgentInput) => Promise<unknown>;
};

export function AgentEditorDialog({
  workspaceId,
  open,
  onClose,
  onSubmit,
}: AgentEditorDialogProps) {
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [selectedVersions, setSelectedVersions] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const optionsQuery = useQuery({
    queryKey: ["agent-skill-options", workspaceId],
    queryFn: () => loadPublishedSkillOptions(workspaceId),
    enabled: open,
  });

  function close() {
    if (!submitting) onClose();
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onSubmit({
        display_name: displayName.trim(),
        description: description.trim(),
        system_prompt: systemPrompt.trim(),
        skill_version_ids: selectedVersions,
      });
      setDisplayName("");
      setDescription("");
      setSystemPrompt("");
      setSelectedVersions([]);
    } catch (error) {
      setSubmitError(
        error instanceof AgentsApiError
          ? error.message
          : "保存失败，请稍后重试。",
      );
    } finally {
      // 弹窗保留挂载以便重开，成功和失败都必须释放提交状态。
      setSubmitting(false);
    }
  }

  function toggleVersion(versionId: string) {
    setSelectedVersions((current) =>
      current.includes(versionId)
        ? current.filter((item) => item !== versionId)
        : [...current, versionId],
    );
  }

  const options = optionsQuery.data ?? [];
  return (
    <Modal
      centered
      closeOnEsc={!submitting}
      footer={null}
      maskClosable={!submitting}
      onCancel={close}
      title="创建 Agent"
      visible={open}
      width={720}
    >
      <form className="agent-form" onSubmit={submit}>
        <Typography.Paragraph type="tertiary">
          先保存草稿；发布后系统会冻结 Prompt 和 SkillVersion 绑定。
        </Typography.Paragraph>
        <label className="form-field">
          <span>Agent 名称</span>
          <Input
            autoFocus
            maxLength={120}
            onChange={setDisplayName}
            required
            value={displayName}
          />
        </label>
        <label className="form-field">
          <span>Agent 描述</span>
          <Input
            maxLength={2000}
            onChange={setDescription}
            required
            value={description}
          />
        </label>
        <label className="form-field">
          <span>System Prompt</span>
          <TextArea
            maxLength={30000}
            onChange={setSystemPrompt}
            required
            rows={6}
            value={systemPrompt}
          />
        </label>

        {/* 使用普通分组容器，避免原生 fieldset 在窄屏弹窗中压缩匿名内容盒。 */}
        <section
          aria-labelledby="agent-skill-picker-title"
          className="skill-picker"
        >
          <div className="skill-picker-header">
            <h3 id="agent-skill-picker-title">绑定已发布 Skill 版本</h3>
            <p>绑定具体版本而不是 latest，确保历史运行可以复现。</p>
          </div>
          {optionsQuery.isPending ? <Spin tip="正在读取已发布版本" /> : null}
          {optionsQuery.isError ? (
            <Banner
              closeIcon={null}
              description="Skill 版本加载失败，请关闭后重试。"
              type="danger"
            />
          ) : null}
          {!optionsQuery.isPending &&
          !optionsQuery.isError &&
          options.length === 0 ? (
            <div className="picker-empty">
              暂无可绑定版本；Agent 也可以先保存为空草稿。
            </div>
          ) : null}
          <div className="skill-option-list">
            {options.map((option) => (
              <div className="skill-option" key={option.version.id}>
                <Checkbox
                  aria-label={`${option.skill.display_name} ${option.version.version}`}
                  checked={selectedVersions.includes(option.version.id)}
                  onChange={() => toggleVersion(option.version.id)}
                >
                  <span className="skill-option-icon">
                    <IconPuzzle />
                  </span>
                  <span>
                    <strong>{option.skill.display_name}</strong>
                    <small>{option.skill.slug}</small>
                  </span>
                  <Tag color="blue">{option.version.version}</Tag>
                </Checkbox>
              </div>
            ))}
          </div>
        </section>

        {submitError ? (
          <Banner closeIcon={null} description={submitError} type="danger" />
        ) : null}
        <div className="modal-actions">
          <Button disabled={submitting} onClick={close}>
            取消
          </Button>
          <Button
            disabled={
              !displayName.trim() || !description.trim() || !systemPrompt.trim()
            }
            htmlType="submit"
            loading={submitting}
            theme="solid"
            type="primary"
          >
            保存草稿
          </Button>
        </div>
      </form>
    </Modal>
  );
}

async function loadPublishedSkillOptions(
  workspaceId: string,
): Promise<SkillOption[]> {
  // 当前 Skill 列表尚未提供聚合版本端点，只对已有发布版本的资产请求版本清单。
  const page = await listSkills(workspaceId, {});
  const publishedSkills = page.items.filter(
    (skill) => skill.status === "active" && skill.latest_published_version,
  );
  const versions = await Promise.all(
    publishedSkills.map((skill) => listSkillVersions(workspaceId, skill.id)),
  );
  return publishedSkills.flatMap(
    (skill, index) =>
      versions[index]?.items.map((version) => ({ skill, version })) ?? [],
  );
}
