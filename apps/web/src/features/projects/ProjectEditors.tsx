import {
  Banner,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  TextArea,
  Typography,
} from "../../components/console";
import { type FormEvent, useState } from "react";
import { errorMessage } from "../runs/api";
import {
  type ProjectInput,
  type Repository,
  type RepositoryInput,
} from "./api";
import type { BuildProfile, CodeSource } from "./codeSourcesApi";
import { useDiscardChanges } from "../settings/useDiscardChanges";
import "./editors.css";

type DialogProps<T> = {
  initial?: T;
  onClose: () => void;
  onSave: (values: T) => Promise<unknown>;
};

export function ProjectEditor({
  initial,
  onClose,
  onSave,
  terminology = "项目",
}: DialogProps<ProjectInput> & { terminology?: string }) {
  const [values, setValues] = useState<ProjectInput>(
    initial ?? { name: "", description: "", architecture: "", conventions: "" },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [baseline] = useState(() => JSON.stringify(values));
  const { requestClose, confirmation } = useDiscardChanges(
    JSON.stringify(values) !== baseline,
    onClose,
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSave(values);
      onClose();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Modal
        visible
        centered
        title={initial ? `编辑${terminology}资料` : `创建${terminology}`}
        footer={null}
        width={720}
        onCancel={() => {
          if (!busy) requestClose();
        }}
        maskClosable={!busy}
        closeOnEsc={!busy}
      >
        <form className="agent-form" onSubmit={submit}>
          <Typography.Paragraph type="tertiary">
            项目资料供团队参考。每次任务冻结选中版本，之后编辑不会改写旧任务。
          </Typography.Paragraph>
          <label className="form-field">
            <span>{terminology}名称</span>
            <Input
              required
              maxLength={120}
              value={values.name}
              onChange={(name) => setValues({ ...values, name })}
            />
          </label>
          <label className="form-field">
            <span>业务说明</span>
            <TextArea
              maxLength={6000}
              rows={3}
              value={values.description}
              onChange={(description) => setValues({ ...values, description })}
              placeholder="业务目标、核心概念和已知约束"
            />
          </label>
          <label className="form-field">
            <span>整体架构</span>
            <TextArea
              maxLength={6000}
              rows={4}
              value={values.architecture}
              onChange={(architecture) =>
                setValues({ ...values, architecture })
              }
              placeholder="例如前后端分离、服务之间的关系、接口约定"
            />
          </label>
          <label className="form-field">
            <span>研发约定</span>
            <TextArea
              maxLength={4000}
              rows={3}
              value={values.conventions}
              onChange={(conventions) => setValues({ ...values, conventions })}
              placeholder="编码约定、验收要求；不填写密钥"
            />
          </label>
          {error && (
            <Banner closeIcon={null} type="danger" description={error} />
          )}
          <div className="modal-actions">
            <Button disabled={busy} onClick={requestClose}>
              取消
            </Button>
            <Button
              theme="solid"
              htmlType="submit"
              loading={busy}
              disabled={!values.name.trim()}
            >
              保存{terminology}
            </Button>
          </div>
        </form>
      </Modal>
      {confirmation}
    </>
  );
}

export function RepositoryEditor({
  current,
  onClose,
  onSave,
  codeSources = [],
  buildProfiles = [],
}: {
  current?: Repository | undefined;
  onClose: () => void;
  onSave: (input: RepositoryInput) => Promise<unknown>;
  codeSources?: CodeSource[];
  buildProfiles?: BuildProfile[];
}) {
  const [values, setValues] = useState<RepositoryInput>(() =>
    current
      ? {
          name: current.name,
          purpose: current.purpose,
          provider: current.provider,
          git_url: current.git_url,
          default_branch: current.default_branch,
          technology: current.technology,
          development_notes: current.development_notes,
          status: current.status,
          credential_username: current.credential_username ?? "",
          code_source_id: current.code_source_id ?? null,
          platform_repository: current.platform_repository ?? "",
          build_config: current.build_config ?? null,
        }
      : {
          name: "",
          purpose: "",
          provider: "git",
          git_url: "",
          default_branch: "main",
          technology: "",
          development_notes: "",
          status: "active",
          credential_username: "",
        },
  );
  // 凭据只保留在本次弹窗内存中，关闭即卸载；不缓存、不回填、不进入任务正文。
  const [credentialSecret, setCredentialSecret] = useState("");
  const [clearToken, setClearToken] = useState(false);
  const [commandText, setCommandText] = useState(
    () =>
      current?.build_config?.commands
        .map((argv) => JSON.stringify(argv))
        .join("\n") ?? "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [baseline] = useState(() => JSON.stringify({ values, commandText }));
  const { requestClose, confirmation } = useDiscardChanges(
    JSON.stringify({ values, commandText }) !== baseline ||
      !!credentialSecret ||
      clearToken,
    onClose,
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const commands: string[][] = values.build_config
        ? commandText
            .split("\n")
            .filter((line) => line.trim())
            .map((line) => {
              const parsed: unknown = JSON.parse(line);
              if (
                !Array.isArray(parsed) ||
                !parsed.length ||
                !parsed.every((arg) => typeof arg === "string" && arg.length)
              ) {
                throw new Error(
                  '构建命令每行填写一个 argv 数组，例如 ["mvn","-o","clean","verify"]',
                );
              }
              return parsed as string[];
            })
        : [];
      await onSave({
        ...values,
        ...(values.build_config
          ? { build_config: { ...values.build_config, commands } }
          : {}),
        ...(clearToken
          ? { credential_token: "" }
          : credentialSecret
            ? { credential_token: credentialSecret }
            : {}),
      });
      setCredentialSecret("");
      onClose();
    } catch (cause) {
      setCredentialSecret("");
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Modal
        visible
        centered
        title={current ? "编辑仓库" : "添加仓库"}
        footer={null}
        width={720}
        onCancel={() => {
          if (!busy) requestClose();
        }}
        maskClosable={!busy}
        closeOnEsc={!busy}
      >
        <form className="agent-form repository-editor" onSubmit={submit}>
          <Banner
            closeIcon={null}
            type="info"
            description="保存不会联网。保存后可校验连接；发起项目任务将自动拉取选中仓库的固定提交供只读工具使用，不执行脚本、子模块或推送。SSH 需管理员配置专用密钥与 known_hosts。"
          />
          <label className="form-field repository-editor-half">
            <span>仓库名称</span>
            <Input
              required
              maxLength={120}
              value={values.name}
              onChange={(name) => setValues({ ...values, name })}
            />
          </label>
          <label className="form-field repository-editor-half">
            <span>仓库用途</span>
            <Input
              maxLength={2000}
              value={values.purpose}
              onChange={(purpose) => setValues({ ...values, purpose })}
              placeholder="例如管理端前端、订单后端、接口定义"
            />
          </label>
          <div className="form-field repository-editor-half">
            <span id="repository-provider-label">托管平台</span>
            <Select
              aria-labelledby="repository-provider-label"
              value={values.provider}
              optionList={[
                { value: "git", label: "通用 Git" },
                { value: "codeup", label: "云效 Codeup" },
                { value: "gitlab", label: "GitLab" },
                { value: "github", label: "GitHub" },
              ]}
              onChange={(provider) =>
                setValues({
                  ...values,
                  provider: provider as RepositoryInput["provider"],
                  code_source_id: null,
                  platform_repository: "",
                })
              }
            />
          </div>
          <label className="form-field repository-editor-half">
            <span>默认分支</span>
            <Input
              required
              maxLength={120}
              value={values.default_branch}
              onChange={(default_branch) =>
                setValues({ ...values, default_branch })
              }
            />
          </label>
          <label className="form-field">
            <span>Git 地址</span>
            <Input
              required
              maxLength={2048}
              value={values.git_url}
              onChange={(git_url) => setValues({ ...values, git_url })}
              placeholder="HTTPS 克隆地址或 git@host:team/repo.git；不要内嵌密码"
            />
          </label>
          <label className="form-field">
            <span>技术栈</span>
            <Input
              maxLength={500}
              value={values.technology}
              onChange={(technology) => setValues({ ...values, technology })}
              placeholder="例如 React / TypeScript、Java / Spring Boot"
            />
          </label>
          <label className="form-field">
            <span>开发说明与命令建议</span>
            <TextArea
              maxLength={4000}
              rows={3}
              value={values.development_notes}
              onChange={(development_notes) =>
                setValues({ ...values, development_notes })
              }
              placeholder="目录、启动及测试说明；这些建议不会自动获得执行批准"
            />
          </label>
          <label className="form-field repository-editor-half">
            <span>Git 认证用户名（私有 HTTPS 仓库）</span>
            <Input
              maxLength={200}
              value={values.credential_username ?? ""}
              onChange={(credential_username) =>
                setValues({ ...values, credential_username })
              }
              placeholder="云效填写对应 Git 认证用户名；公开仓库可留空"
            />
          </label>
          <label className="form-field repository-editor-half">
            <span>Git 密码或访问令牌（选填，只写）</span>
            <Input
              className="project-secret-input"
              aria-label="Git 密码或访问令牌（选填，只写）"
              type="password"
              autoComplete="new-password"
              maxLength={8000}
              disabled={clearToken}
              value={credentialSecret}
              onChange={setCredentialSecret}
              placeholder={
                current?.has_credential
                  ? "已保存；留空不更改"
                  : "经平台加密保存，不会交给模型"
              }
            />
          </label>
          <Typography.Paragraph type="tertiary">
            HTTPS 支持 Git 用户名配合 Git 密码或访问令牌，不强制使用令牌。
            请填写代码托管平台提供的 Git 认证信息，不要把密码拼进 Git 地址。
            云效官方仓库网页地址会自动规范为以 .git 结尾的克隆地址。 修改 Git
            地址会清除旧凭据，需重新填写。
          </Typography.Paragraph>
          {current?.has_credential && (
            <Checkbox
              checked={clearToken}
              onChange={(event) => setClearToken(!!event.target.checked)}
            >
              清除已保存凭据
            </Checkbox>
          )}
          <details className="repository-advanced">
            <summary>构建环境与 MR 交付</summary>
            <p className="form-help">
              这里只保存项目建议。执行命令、推送分支和创建 MR 仍需逐次审批。
            </p>
            <label className="form-field">
              <span>构建环境</span>
              <Select
                aria-label="构建环境"
                value={values.build_config?.profile ?? "legacy"}
                optionList={[
                  { value: "legacy", label: "沿用部署默认环境" },
                  ...buildProfiles.map((profile) => ({
                    value: profile.id,
                    label: profile.name,
                  })),
                ]}
                onChange={(profile) =>
                  setValues({
                    ...values,
                    build_config:
                      profile === "legacy"
                        ? null
                        : {
                            profile: profile as "default" | "project",
                            commands: values.build_config?.commands ?? [],
                          },
                  })
                }
              />
            </label>
            {values.build_config && (
              <label className="form-field">
                <span>建议构建命令（每行一个 argv 数组）</span>
                <TextArea
                  rows={4}
                  value={commandText}
                  onChange={setCommandText}
                  maxLength={8000}
                  placeholder={'["mvn","-o","-B","clean","verify"]'}
                />
              </label>
            )}
            <label className="form-field">
              <span>MR API 连接</span>
              <Select
                aria-label="MR API 连接"
                disabled={values.provider !== "codeup"}
                value={values.code_source_id ?? "none"}
                optionList={[
                  { value: "none", label: "不关联，保留部署级配置" },
                  ...codeSources
                    .filter((source) => source.enabled && source.has_credential)
                    .map((source) => ({
                      value: source.id,
                      label: source.name,
                    })),
                ]}
                onChange={(value) =>
                  setValues({
                    ...values,
                    code_source_id: value === "none" ? null : String(value),
                    platform_repository:
                      value === "none"
                        ? ""
                        : (values.platform_repository ?? ""),
                  })
                }
              />
            </label>
            {values.code_source_id && (
              <label className="form-field">
                <span>云效平台仓库 ID</span>
                <Input
                  required
                  value={values.platform_repository ?? ""}
                  maxLength={20}
                  pattern="[1-9][0-9]{0,19}"
                  onChange={(platform_repository) =>
                    setValues({ ...values, platform_repository })
                  }
                  placeholder="云效仓库数字 ID，不是 Git 地址"
                />
              </label>
            )}
            <p className="form-help">
              Git 账号密码只用于克隆与推送。MR 使用独立 API
              身份；其他托管平台暂不启用 MR API。
            </p>
          </details>
          {current && (
            <div className="form-field">
              <span id="repository-status-label">仓库状态</span>
              <Select
                aria-labelledby="repository-status-label"
                value={values.status}
                optionList={[
                  { value: "active", label: "活动" },
                  { value: "disabled", label: "停用（立即阻断关联任务访问）" },
                ]}
                onChange={(status) =>
                  setValues({
                    ...values,
                    status: status as Repository["status"],
                  })
                }
              />
            </div>
          )}
          {error && (
            <Banner closeIcon={null} type="danger" description={error} />
          )}
          <div className="modal-actions">
            <Button disabled={busy} onClick={requestClose}>
              取消
            </Button>
            <Button
              theme="solid"
              htmlType="submit"
              loading={busy}
              disabled={!values.name.trim() || !values.git_url.trim()}
            >
              保存仓库
            </Button>
          </div>
        </form>
      </Modal>
      {confirmation}
    </>
  );
}
