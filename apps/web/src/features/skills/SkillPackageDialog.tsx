import { File as IconFile, Upload as IconUpload } from "lucide-react";
import { Banner, Button, Modal, Typography } from "../../components/console";
import { useRef, useState, type DragEvent, type FormEvent } from "react";

import { SkillsApiError } from "./api";

type SkillPackageDialogProps = {
  title: string;
  submitLabel: string;
  open: boolean;
  onClose: () => void;
  onSubmit: (file: File) => Promise<void>;
};

const MAX_PACKAGE_BYTES = 50 * 1024 * 1024;

export function SkillPackageDialog({
  title,
  submitLabel,
  open,
  onClose,
  onSubmit,
}: SkillPackageDialogProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [dragging, setDragging] = useState(false);

  function close() {
    if (submitting) return;
    setFile(null);
    setError(null);
    setDragging(false);
    onClose();
  }

  function selectFile(nextFile: File | undefined) {
    setError(null);
    if (!nextFile) return;
    // 替换文件失败时清除旧选择，不能把上一次的有效文件误当作新文件提交。
    setFile(null);
    if (
      !nextFile.name.toLowerCase().endsWith(".zip") &&
      !nextFile.name.toLowerCase().endsWith(".skill")
    ) {
      setError("仅支持 .zip 或 .skill 文件");
      return;
    }
    if (nextFile.size > MAX_PACKAGE_BYTES) {
      setError("文件不能超过 50 MB");
      return;
    }
    setFile(nextFile);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (submitting) return;
    setDragging(false);
    selectFile(event.dataTransfer.files[0]);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    if (!file) {
      setError("请先选择一个 Skill 包");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(file);
      setFile(null);
    } catch (nextError) {
      setError(
        nextError instanceof SkillsApiError
          ? nextError.message
          : "上传未完成，请稍后重试",
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
      onCancel={close}
      title={title}
      visible={open}
      width={620}
    >
      <form className="package-form" onSubmit={handleSubmit}>
        <div
          className={
            dragging ? "package-dropzone dragging" : "package-dropzone"
          }
          onDragEnter={() => {
            if (!submitting) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDragOver={(event) => event.preventDefault()}
          onDrop={handleDrop}
        >
          <input
            hidden
            disabled={submitting}
            accept=".zip,.skill,application/zip"
            onChange={(event) => {
              selectFile(event.target.files?.[0]);
              // 文件由 React 状态持有，清空原生值后仍可重新选择同一个包。
              event.target.value = "";
            }}
            ref={inputRef}
            type="file"
          />
          <span className="upload-mark" aria-hidden="true">
            {file ? <IconFile /> : <IconUpload />}
          </span>
          {file && <strong>{file.name}</strong>}
          <Button
            disabled={submitting}
            onClick={() => inputRef.current?.click()}
          >
            {file ? "重新选择文件" : "选择 .zip 或 .skill 文件"}
          </Button>
          <small>
            {file
              ? formatBytes(file.size)
              : "选择或拖入完整 Skill 目录压缩包，最大 50 MB"}
          </small>
        </div>

        <div className="package-guidance">
          <Typography.Text strong>包结构要求</Typography.Text>
          <Typography.Text type="tertiary">
            一个顶层目录，根目录内包含带 name 和 description 的 SKILL.md。
          </Typography.Text>
        </div>
        {error ? (
          <Banner closeIcon={null} description={error} type="danger" />
        ) : null}
        <div className="modal-actions">
          <Button disabled={submitting} onClick={close} theme="light">
            取消
          </Button>
          <Button
            disabled={!file}
            htmlType="submit"
            loading={submitting}
            theme="solid"
            type="primary"
          >
            {submitting ? "正在校验并上传…" : submitLabel}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
