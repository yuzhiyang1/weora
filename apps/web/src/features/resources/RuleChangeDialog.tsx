import { useMutation } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";
import { Banner, Button, Input, Modal, Select, TextArea } from "../../components/console";
import { proposeRuleChange, type ResourceChange, type ResourceSnapshot, type RuleChangeFiles, type TeamRule } from "./api";

export type RuleOperation = { kind: "create" } | { kind: "edit" | "delete"; rule: TeamRule };

const segment = /^[\p{L}\p{N}][\p{L}\p{N}_-]{0,79}$/u;
const title = { create: "新增规则", edit: "修改规则", delete: "删除规则" } as const;

export function RuleChangeDialog({ workspace, snapshot, operation, onClose, onSubmitted }: {
  workspace: string;
  snapshot: ResourceSnapshot;
  operation: RuleOperation;
  onClose: () => void;
  onSubmitted: (change: ResourceChange) => void;
}) {
  const rule = operation.kind === "create" ? null : operation.rule;
  // 每次打开独立候选分支。网络丢失响应后，重复提交同一草稿会取回已有请求。
  const [branch] = useState(() => {
    const random = crypto.getRandomValues(new Uint8Array(16));
    return `rules/web-${Array.from(random, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
  });
  const [name, setName] = useState("");
  const [namespace, setNamespace] = useState("");
  const [body, setBody] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [sourcePath, setSourcePath] = useState(rule?.path ?? "");
  const [validation, setValidation] = useState("");
  const sourceLabel = useId();
  const submit = useMutation({
    mutationFn: (files: RuleChangeFiles) => proposeRuleChange(workspace, branch, snapshot.revision, files),
    onSuccess: onSubmitted,
  });

  const folder = rule?.path.endsWith("/RULE.md") ? rule.path.slice(0, -"RULE.md".length) : null;
  // 目录型规则的翻译和附件属于同一规则。编辑只涉及 Markdown 原文，删除则提交完整目录。
  const ruleFiles = folder ? Object.keys(snapshot.files).filter((path) => path.startsWith(folder)) : rule ? [rule.path] : [];
  const editableFiles = ruleFiles.filter((path) =>
    path === rule?.path || (folder && /^RULE\.[^/]+\.md$/.test(path.slice(folder.length))),
  );
  const original = snapshot.files[sourcePath];
  const originalText = typeof original === "string" ? original : "";

  function submitForm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submit.isPending) return;
    setValidation("");
    submit.reset();

    if (operation.kind === "delete") {
      submit.mutate(Object.fromEntries(ruleFiles.map((path) => [path, null])));
      return;
    }
    if (operation.kind === "edit") {
      const updates = Object.entries(edits).filter(([path, value]) => value !== snapshot.files[path]);
      if (updates.length) submit.mutate(Object.fromEntries(updates));
      return;
    }

    const leaf = name.trim();
    const scope = namespace.trim();
    if (!segment.test(leaf) || (scope && !segment.test(scope))) {
      setValidation("规则名称和命名空间限 80 个字符，以字母或数字开头，可包含短横线和下划线。");
      return;
    }
    if (leaf.toLowerCase() === "rule") {
      setValidation("RULE 是目录型规则的保留文件名，请使用其他名称。");
      return;
    }
    const key = scope ? `${scope}/${leaf}` : leaf;
    const path = `rules/${key}.md`;
    const normalizedPath = path.toLowerCase();
    // 避免同名规则，也避免把新文件放进已有规则目录而被识别成附件。
    if (snapshot.rules.some((entry) => entry.name.toLowerCase() === key.toLowerCase()
      || (entry.path.endsWith("/RULE.md") && normalizedPath.startsWith(entry.path.toLowerCase().slice(0, -"RULE.md".length))))
      || Object.keys(snapshot.files).some((file) => file.toLowerCase() === normalizedPath)) {
      setValidation("此名称已被规则占用，或位于已有规则的附件目录。请换一个名称或命名空间。");
      return;
    }
    if (body.trim()) submit.mutate({ [path]: body });
  }

  const canSubmit = operation.kind === "create" ? Boolean(name.trim() && body.trim())
    : operation.kind === "delete" ? ruleFiles.length > 0
    : Object.entries(edits).some(([path, value]) => value !== snapshot.files[path]);
  const pending = submit.isPending;

  return (
    <Modal title={title[operation.kind]} visible width={720} maskClosable={!pending} closeOnEsc={!pending}
      onCancel={() => { if (!pending) onClose(); }}>
      <form className="resource-dialog" onSubmit={submitForm}>
        {operation.kind === "create" && <div className="resource-rule-fields">
          <label className="form-field"><span>规则名称</span>
            <Input autoFocus value={name} onChange={setName} maxLength={80} required disabled={pending} placeholder="例如 java-style" />
          </label>
          <label className="form-field"><span>命名空间</span>
            <Input value={namespace} onChange={setNamespace} maxLength={80} disabled={pending} placeholder="留空则为共享规则" />
          </label>
        </div>}
        {operation.kind === "edit" && (editableFiles.length > 1 ? <div className="form-field">
          <span id={sourceLabel}>规则文件</span>
          <Select aria-labelledby={sourceLabel} value={sourcePath} disabled={pending}
            optionList={editableFiles.map((path) => ({ value: path, label: path.slice(folder?.length ?? 0) }))}
            onChange={(path) => { if (typeof path === "string") setSourcePath(path); }} />
        </div> : <code className="resource-rule-path">{rule?.path}</code>)}
        {operation.kind === "delete" ? <>
          <p>合并后，「{rule?.name}」会从已发布规则中移除，成员下次同步时会清理已安装副本。</p>
          {folder && <p>本次共移除 {ruleFiles.length} 个文件，包括规则目录中的翻译和附件。</p>}
        </> : <label className="form-field"><span>Markdown 原文</span>
          <TextArea rows={10} disabled={pending} value={operation.kind === "create" ? body : (edits[sourcePath] ?? originalText)}
            onChange={operation.kind === "create" ? setBody : (value) => setEdits({ ...edits, [sourcePath]: value })} />
        </label>}
        {(validation || submit.error) && <Banner type="danger" description={validation || submit.error?.message} />}
        <div className="resource-actions">
          <Button theme="light" disabled={pending} onClick={onClose}>取消</Button>
          <Button htmlType="submit" theme="solid" type={operation.kind === "delete" ? "danger" : "primary"}
            loading={pending} disabled={!canSubmit || pending}>
            {operation.kind === "delete" ? "提交删除请求" : "提交合并请求"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
