import { useDiscardChanges } from "../settings/useDiscardChanges";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box,
  ChevronRight,
  GitBranch,
  KeyRound,
  Plus,
  Search,
  Settings2,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  Banner,
  Button,
  Checkbox,
  Empty,
  Input,
  Modal,
  Select,
  Skeleton,
  Tag,
} from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { errorMessage } from "../runs/api";
import {
  checkRepository,
  listProjects,
  projectKey,
  saveRepository,
  type Repository,
} from "./api";
import {
  codeSourceKey,
  listBuildProfiles,
  listLegacyCodeSources,
  importLegacyCodeSource,
  listCodeSources,
  listWorkspaceRepositories,
  saveCodeSource,
  type CodeSource,
  type CodeSourceInput,
  type LegacyCodeSource,
} from "./codeSourcesApi";
import { RepositoryEditor } from "./ProjectEditors";
import "./code-settings.css";

/** 设置总览组织现有领域能力，不把工作空间连接等同于所有项目的执行授权。 */
export function CodeSettingsPage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("代码设置缺少身份");
  return (
    <CodeSettingsContent
      key={`${principal.workspace.id}:${principal.user.id}`}
    />
  );
}

function CodeSettingsContent() {
  const { principal } = useAuth();
  if (!principal) throw new Error("代码设置缺少身份");
  const { workspace, user } = principal;
  const key = codeSourceKey(workspace.id, user.id);
  const client = useQueryClient();
  const sources = useQuery({
    queryKey: key,
    queryFn: () => listCodeSources(workspace.id),
  });
  const legacy = useQuery({
    queryKey: [...key, "legacy"],
    queryFn: () => listLegacyCodeSources(workspace.id),
    retry: false,
  });
  const [legacyConfirm, setLegacyConfirm] = useState<LegacyCodeSource | null>(
    null,
  );
  const repositories = useQuery({
    queryKey: [...key, "repositories"],
    queryFn: () => listWorkspaceRepositories(workspace.id),
  });
  const profiles = useQuery({
    queryKey: [...key, "profiles"],
    queryFn: () => listBuildProfiles(workspace.id),
  });
  const projects = useQuery({
    queryKey: projectKey(workspace.id, user.id),
    queryFn: () => listProjects(workspace.id),
  });
  const [sourceEditor, setSourceEditor] = useState<CodeSource | "new" | null>(
    null,
  );
  const [repositoryEditor, setRepositoryEditor] = useState<{
    project: string;
    current?: Repository;
  } | null>(null);
  const [choosingProject, setChoosingProject] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const rows = (repositories.data?.items ?? []).filter((row) =>
    `${row.name} ${row.project_name} ${row.git_url}`
      .toLowerCase()
      .includes(search.trim().toLowerCase()),
  );
  async function refresh() {
    await Promise.all([
      client.invalidateQueries({ queryKey: key }),
      client.invalidateQueries({ queryKey: projectKey(workspace.id, user.id) }),
    ]);
  }
  return (
    <div className="code-settings-layout page-enter">
      <aside className="settings-local-nav" aria-label="工作空间设置">
        <h2>设置</h2>
        <span>连接与执行</span>
        <Link className="active" aria-current="page" to="/settings/code">
          <GitBranch size={16} />
          代码
        </Link>
        <Link to="/context">
          <Settings2 size={16} />
          飞书上下文
        </Link>
      </aside>
      <section className="code-settings-page">
        <header className="code-settings-header">
          <div>
            <p>工作空间设置</p>
            <h1>代码</h1>
          </div>
          <Tag>{workspace.name}</Tag>
        </header>
        {actionError && (
          <Banner type="danger" description={actionError} closeIcon={null} />
        )}
        <section
          aria-labelledby="hosting-heading"
          className="code-settings-section"
        >
          <div className="code-section-heading">
            <div>
              <h2 id="hosting-heading">代码托管</h2>
              <p>集中管理 MR API 身份。克隆与推送使用仓库各自的 Git 凭据。</p>
            </div>
            <Button
              onClick={() => setSourceEditor("new")}
              icon={<Plus size={15} />}
            >
              添加代码源
            </Button>
          </div>
          {legacy.isError && (
            <Banner
              type="warning"
              description="已有部署配置读取失败，请刷新重试；不影响手工配置。"
              closeIcon={null}
            />
          )}
          {(legacy.data?.items ?? []).map((candidate) => (
            <div className="hosting-row" key={candidate.fingerprint}>
              <div className="hosting-copy">
                <strong>发现已有配置 · {candidate.repository_name}</strong>
                <p>
                  复用后端登记的云效 API 身份，保留仓库 Git
                  凭据，无需重新填写令牌。
                </p>
              </div>
              <Button
                disabled={busy !== null}
                onClick={() => setLegacyConfirm(candidate)}
              >
                接入已有配置
              </Button>
            </div>
          ))}
          {sources.isError ? (
            <Banner
              type="danger"
              description={errorMessage(sources.error)}
              closeIcon={null}
            />
          ) : sources.isPending ? (
            <Skeleton />
          ) : (
            <div className="hosting-connections">
              {!sources.data.items.length && (
                <div className="hosting-row">
                  <span className="hosting-icon">
                    <GitBranch size={20} />
                  </span>
                  <div className="hosting-copy">
                    <strong>云效 Codeup</strong>
                    <p>尚未配置 MR API 连接，已有 Git 仓库仍可独立使用。</p>
                  </div>
                  <Button onClick={() => setSourceEditor("new")}>
                    配置云效
                  </Button>
                </div>
              )}
              {sources.data.items.map((source) => (
                <div className="hosting-row" key={source.id}>
                  <span className="hosting-icon">
                    <GitBranch size={20} />
                  </span>
                  <div className="hosting-copy">
                    <strong>{source.name}</strong>
                    <p>云效 · {source.organization}</p>
                    <small>
                      {source.enabled
                        ? source.has_credential
                          ? "API 凭据已保存 · 远端未验证"
                          : "未保存 API 凭据"
                        : "已停用"}
                    </small>
                  </div>
                  {source.can_manage && (
                    <Button
                      aria-label={`编辑代码源 ${source.name}`}
                      onClick={() => setSourceEditor(source)}
                    >
                      配置连接
                    </Button>
                  )}
                </div>
              ))}
              <p className="hosting-support">
                支持使用 GitHub、GitLab 等平台的 Git 地址；当前 MR API
                连接支持云效。
              </p>
            </div>
          )}
        </section>
        <section
          aria-labelledby="repositories-heading"
          className="code-settings-section"
        >
          <div className="code-section-heading">
            <div>
              <h2 id="repositories-heading">代码仓库</h2>
              <p>仓库归属项目；Agent 只使用本次任务明确选中的仓库。</p>
            </div>
            <Button
              theme="solid"
              icon={<Plus size={15} />}
              onClick={() => setChoosingProject(true)}
            >
              添加仓库
            </Button>
          </div>
          <Input
            aria-label="搜索代码仓库"
            value={search}
            onChange={setSearch}
            prefix={<Search size={15} />}
            placeholder="搜索仓库、项目或 Git 地址"
          />
          {repositories.isError ? (
            <Banner
              type="danger"
              description={errorMessage(repositories.error)}
              closeIcon={null}
            />
          ) : repositories.isPending ? (
            <Skeleton />
          ) : (
            <div className="settings-repositories">
              {!rows.length && (
                <Empty
                  title={search ? "没有匹配的仓库" : "尚未添加仓库"}
                  description="先创建项目，再登记一个或多个 Git 仓库。"
                />
              )}
              {rows.map((row) => (
                <article className="settings-repository-row" key={row.id}>
                  <span className="hosting-icon">
                    <Box size={18} />
                  </span>
                  <div className="repository-row-main">
                    <Link to={`/projects/${row.project_id}`}>
                      <span>{row.project_name}</span>
                      <ChevronRight size={12} />
                      <strong>{row.name}</strong>
                    </Link>
                    <code title={row.git_url}>{row.git_url}</code>
                    <div className="repository-meta">
                      <span>{row.default_branch}</span>
                      <span>{row.technology || "技术栈未填写"}</span>
                      <span>
                        {row.build_config ? "已指定构建环境" : "部署默认环境"}
                      </span>
                      <span>
                        {row.code_source_id ? "关联 MR API" : "MR API 尚未关联"}
                      </span>
                    </div>
                  </div>
                  <div className="repository-row-actions">
                    <Tag
                      color={
                        row.connection_status === "connected" ? "green" : "grey"
                      }
                    >
                      {row.status === "disabled" ||
                      row.project_status === "archived"
                        ? "不可用"
                        : row.connection_status === "connected"
                          ? "Git 可读"
                          : row.connection_status === "failed"
                            ? "Git 校验失败"
                            : "Git 待校验"}
                    </Tag>
                    {row.can_manage && (
                      <>
                        <Button
                          size="small"
                          loading={busy === row.id}
                          disabled={
                            busy !== null ||
                            row.status !== "active" ||
                            row.project_status !== "active"
                          }
                          aria-label={`校验 Git ${row.name}`}
                          onClick={async () => {
                            setBusy(row.id);
                            setActionError(null);
                            try {
                              const result = await checkRepository(
                                workspace.id,
                                row.project_id,
                                row,
                              );
                              if (result.status !== "connected")
                                setActionError(
                                  result.message ?? "Git 校验失败",
                                );
                              await refresh();
                            } catch (cause) {
                              setActionError(errorMessage(cause));
                            } finally {
                              setBusy(null);
                            }
                          }}
                        >
                          校验
                        </Button>
                        <Button
                          size="small"
                          disabled={row.project_status !== "active"}
                          aria-label={`配置仓库 ${row.name}`}
                          onClick={() =>
                            setRepositoryEditor({
                              project: row.project_id,
                              current: row,
                            })
                          }
                        >
                          配置
                        </Button>
                      </>
                    )}
                  </div>
                </article>
              ))}
            </div>
          )}
          {repositories.data?.truncated && (
            <p className="form-help">
              仅展示前 200 个仓库，请到项目详情查看其他仓库。
            </p>
          )}
        </section>
        <section
          aria-labelledby="delivery-heading"
          className="code-settings-section"
        >
          <div className="code-section-heading">
            <div>
              <h2 id="delivery-heading">交付与安全边界</h2>
              <p>这些是平台约束，不是可以跳过审批的偏好开关。</p>
            </div>
            <KeyRound size={18} />
          </div>
          <div className="delivery-rules">
            <div>
              <strong>构建与测试</strong>
              <p>只在隔离沙箱执行有效批准的命令；仓库配置仅提供建议。</p>
              <span>逐次审批</span>
            </div>
            <div>
              <strong>分支与 MR</strong>
              <p>推送到专用分支，创建普通 MR 需要单独审批。</p>
              <span>不自动合并</span>
            </div>
            <div>
              <strong>凭据与任务</strong>
              <p>凭据加密保存、不回填、不进入模型、沙箱或构建日志。</p>
              <span>只写凭据</span>
            </div>
          </div>
        </section>
      </section>
      {legacyConfirm && (
        <Modal
          visible
          title="接入已有云效配置"
          footer={null}
          maskClosable={busy === null}
          closeOnEsc={busy === null}
          onCancel={() => {
            if (busy === null) setLegacyConfirm(null);
          }}
        >
          <div className="agent-form">
            <p>
              仓库：{legacyConfirm.repository_name}；组织：
              {legacyConfirm.organization}
            </p>
            <p>
              仅在后端将已登记的 API
              令牌加密保存为工作空间共享代码源，并关联原仓库。Git
              密码、构建环境不变；旧任务需重新发起和审批。本次不会推送或创建
              MR。
            </p>
            <Button
              theme="solid"
              loading={busy === "legacy"}
              disabled={busy !== null}
              onClick={async () => {
                if (busy !== null) return;
                setBusy("legacy");
                setActionError(null);
                try {
                  await importLegacyCodeSource(workspace.id, legacyConfirm);
                  await refresh();
                  setLegacyConfirm(null);
                } catch (cause) {
                  setActionError(errorMessage(cause));
                } finally {
                  setBusy(null);
                }
              }}
            >
              确认接入并关联
            </Button>
            {actionError && (
              <Banner
                type="danger"
                description={actionError}
                closeIcon={null}
              />
            )}
          </div>
        </Modal>
      )}
      {sourceEditor && (
        <CodeSourceEditor
          current={sourceEditor === "new" ? undefined : sourceEditor}
          onClose={() => setSourceEditor(null)}
          onSave={async (input) => {
            await saveCodeSource(
              workspace.id,
              input,
              sourceEditor === "new" ? undefined : sourceEditor,
            );
            await refresh();
          }}
        />
      )}
      {choosingProject && (
        <Modal
          visible
          centered
          title="选择仓库所属项目"
          footer={null}
          onCancel={() => setChoosingProject(false)}
        >
          <div className="agent-form">
            {projects.isError ? (
              <Banner
                type="danger"
                description={errorMessage(projects.error)}
              />
            ) : (
              <>
                <label className="form-field">
                  <span>所属项目</span>
                  <Select
                    aria-label="所属项目"
                    value={projectId}
                    optionList={(projects.data?.items ?? [])
                      .filter(
                        (project) =>
                          project.can_manage && project.status === "active",
                      )
                      .map((project) => ({
                        value: project.id,
                        label: project.name,
                      }))}
                    onChange={(value) => setProjectId(String(value))}
                  />
                </label>
                <p className="form-help">
                  前后端分离项目可添加多个仓库。
                  <Link to="/projects">前往项目管理</Link>
                </p>
                <Button
                  theme="solid"
                  disabled={
                    !projectId ||
                    !projects.data?.items.some(
                      (project) =>
                        project.id === projectId &&
                        project.can_manage &&
                        project.status === "active",
                    )
                  }
                  onClick={() => {
                    setChoosingProject(false);
                    setRepositoryEditor({ project: projectId });
                  }}
                >
                  继续填写 Git 地址
                </Button>
              </>
            )}
          </div>
        </Modal>
      )}
      {repositoryEditor && (
        <RepositoryEditor
          key={`${repositoryEditor.project}:${repositoryEditor.current?.id ?? "new"}`}
          current={repositoryEditor.current}
          codeSources={sources.data?.items ?? []}
          buildProfiles={profiles.data?.items ?? []}
          onClose={() => setRepositoryEditor(null)}
          onSave={async (input) => {
            if (sources.isError || profiles.isError)
              throw new Error("配置选项加载失败，请刷新后重试");
            await saveRepository(
              workspace.id,
              repositoryEditor.project,
              input,
              repositoryEditor.current,
            );
            await refresh();
          }}
        />
      )}
    </div>
  );
}

export function CodeSourceEditor({
  current,
  onClose,
  onSave,
}: {
  current?: CodeSource | undefined;
  onClose: () => void;
  onSave: (input: CodeSourceInput) => Promise<unknown>;
}) {
  const [name, setName] = useState(current?.name ?? "云效团队连接");
  const [organization, setOrganization] = useState(current?.organization ?? "");
  const [origin, setOrigin] = useState(
    current?.api_origin ?? "https://openapi-rdc.aliyuncs.com",
  );
  const [enabled, setEnabled] = useState(current?.enabled ?? true);
  const [secret, setSecret] = useState("");
  const [clear, setClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [baseline] = useState(() =>
    JSON.stringify({ name, organization, origin, enabled }),
  );
  const { requestClose, confirmation } = useDiscardChanges(
    JSON.stringify({ name, organization, origin, enabled }) !== baseline ||
      !!secret ||
      clear,
    onClose,
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSave({
        name: name.trim(),
        provider: "codeup",
        organization,
        api_origin: origin,
        enabled,
        ...(clear ? { api_token: "" } : secret ? { api_token: secret } : {}),
      });
      setSecret("");
      onClose();
    } catch (cause) {
      setSecret("");
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
        title={current ? "配置云效连接" : "添加云效代码源"}
        footer={null}
        width={600}
        onCancel={() => {
          if (!busy) requestClose();
        }}
        maskClosable={!busy}
        closeOnEsc={!busy}
      >
        <form className="agent-form" onSubmit={submit}>
          <p className="form-help">
            连接是本工作空间的共享 API
            身份。项目管理者可显式关联同组织仓库；保存不发送远端请求，也不代表令牌已验证。
          </p>
          <label className="form-field">
            <span>连接名称</span>
            <Input required value={name} onChange={setName} maxLength={120} />
          </label>
          <label className="form-field">
            <span>云效组织 ID</span>
            <Input
              required
              value={organization}
              onChange={setOrganization}
              maxLength={100}
              pattern={"[A-Za-z0-9_\\-]+"}
              placeholder="Git 地址中的组织标识"
            />
          </label>
          <label className="form-field">
            <span>官方 API 地址</span>
            <Input
              required
              value={origin}
              onChange={setOrigin}
              maxLength={200}
            />
          </label>
          <label className="form-field">
            <span>MR API 访问令牌（只写）</span>
            <Input
              aria-label="MR API 访问令牌（只写）"
              type="password"
              autoComplete="new-password"
              required={!current}
              disabled={clear}
              value={secret}
              onChange={setSecret}
              maxLength={8000}
              placeholder={
                current?.has_credential
                  ? "已保存；留空不更改"
                  : "不是 Git 密码，也不是 SSH 密钥"
              }
            />
          </label>
          {current?.has_credential && (
            <Checkbox
              checked={clear}
              onChange={(event) => setClear(!!event.target.checked)}
            >
              清除已保存 API 令牌
            </Checkbox>
          )}
          <Checkbox
            checked={enabled}
            onChange={(event) => setEnabled(!!event.target.checked)}
          >
            启用此代码源
          </Checkbox>
          <p className="form-help">
            停用、清除或更新连接会使旧任务的关联授权失效，需要重新发起任务和审批。
          </p>
          {error && (
            <Banner type="danger" description={error} closeIcon={null} />
          )}
          <div className="modal-actions">
            <Button disabled={busy} onClick={requestClose}>
              取消
            </Button>
            <Button
              theme="solid"
              htmlType="submit"
              loading={busy}
              disabled={!name.trim() || !organization}
            >
              保存连接
            </Button>
          </div>
        </form>
      </Modal>
      {confirmation}
    </>
  );
}
