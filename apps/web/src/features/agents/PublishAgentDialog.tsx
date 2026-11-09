import {
  Banner,
  Button,
  Input,
  Modal,
  TextArea,
  Typography,
} from "../../components/console";
import { type FormEvent, useState } from "react";

import { AgentsApiError } from "./api";

type PublishAgentDialogProps = {
  open: boolean;
  onClose: () => void;
  onSubmit: (input: { version: string; changelog: string }) => Promise<unknown>;
};

export function PublishAgentDialog({
  open,
  onClose,
  onSubmit,
}: PublishAgentDialogProps) {
  const [version, setVersion] = useState("");
  const [changelog, setChangelog] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({ version: version.trim(), changelog: changelog.trim() });
      setVersion("");
      setChangelog("");
    } catch (reason) {
      setError(
        reason instanceof AgentsApiError
          ? reason.message
          : "发布失败，请稍后重试。",
      );
    } finally {
      // 弹窗保留挂载以便重开，成功和失败都必须释放提交状态。
      setSubmitting(false);
    }
  }

  return (
    <Modal
      centered
      closeOnEsc={!submitting}
      footer={null}
      maskClosable={!submitting}
      onCancel={onClose}
      title="发布 Agent 版本"
      visible={open}
      width={560}
    >
      <form className="agent-form" onSubmit={submit}>
        <Typography.Paragraph type="tertiary">
          发布后 Prompt 与 SkillVersion 绑定不可修改。
        </Typography.Paragraph>
        <label className="form-field">
          <span>版本号</span>
          <Input
            onChange={setVersion}
            placeholder="0.1.0"
            required
            value={version}
          />
        </label>
        <label className="form-field">
          <span>变更说明</span>
          <TextArea
            onChange={setChangelog}
            required
            rows={4}
            value={changelog}
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
            disabled={!version.trim() || !changelog.trim()}
            htmlType="submit"
            loading={submitting}
            theme="solid"
            type="primary"
          >
            确认发布
          </Button>
        </div>
      </form>
    </Modal>
  );
}
