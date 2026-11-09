import {
  Banner,
  Button,
  Empty,
  Table,
  Tag,
  Typography,
} from "../../components/console";
import {
  AssetBackLink,
  AssetDetailsLink,
  AssetLoadError,
} from "../../components/console/asset-controls";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { errorMessage } from "../runs/api";
import {
  createProject,
  checkRepository,
  editProject,
  getProject,
  listProjects,
  projectKey,
  saveRepository,
  type Project,
  type Repository,
} from "./api";
import { ProjectEditor, RepositoryEditor } from "./ProjectEditors";
import {
  codeSourceKey,
  listBuildProfiles,
  listCodeSources,
} from "./codeSourcesApi";
import "./projects.css";

export function ProjectsPage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("项目页面缺少当前身份");
  const { workspace, user } = principal;
  const [editing, setEditing] = useState(false);
  const client = useQueryClient();
  const projects = useQuery({
    queryKey: projectKey(workspace.id, user.id),
    queryFn: () => listProjects(workspace.id),
  });
  return (
    <section className="asset-page asset-mobile-list page-enter">
      <header className="page-header">
        <div>
          <span className="page-eyebrow">PROJECTS / REPOSITORIES</span>
          <Typography.Title heading={1}>项目</Typography.Title>
          <Typography.Paragraph>
            维护团队业务背景，为一个项目绑定多个前端、后端或其他仓库。
          </Typography.Paragraph>
        </div>
        <Button size="large" theme="solid" onClick={() => setEditing(true)}>
          创建项目
        </Button>
      </header>
      <section className="asset-surface">
        {projects.isError ? (
          <AssetLoadError
            title="项目暂时无法加载"
            description={errorMessage(projects.error)}
            onRetry={() => void projects.refetch()}
          />
        ) : !projects.isPending && !projects.data?.items.length ? (
          <div className="asset-empty">
            <Empty
              title="还没有项目"
              description="先创建项目，再登记团队仓库与开发说明。"
            />
          </div>
        ) : (
          <Table
            className="asset-table asset-stack-table"
            variant="secondary"
            rowKey="id"
            loading={projects.isPending}
            dataSource={projects.data?.items ?? []}
            pagination={false}
            columns={[
              {
                title: "项目",
                render: (_: unknown, item: Project) => (
                  <Link className="resource-cell" to={`/projects/${item.id}`}>
                    <span>
                      <strong>{item.name}</strong>
                      <small>{item.description || "未填写业务说明"}</small>
                    </span>
                  </Link>
                ),
              },
              {
                title: "状态",
                render: (_: unknown, item: Project) => (
                  <Tag color={item.status === "active" ? "green" : "grey"}>
                    {item.status === "active" ? "活动" : "已归档"}
                  </Tag>
                ),
              },
              {
                title: "资料版本",
                render: (_: unknown, item: Project) => `v${item.version}`,
              },
              {
                title: "",
                width: 106,
                render: (_: unknown, item: Project) => (
                  <AssetDetailsLink
                    to={`/projects/${item.id}`}
                    name={item.name}
                  />
                ),
              },
            ]}
          />
        )}
      </section>
      {editing && (
        <ProjectEditor
          onClose={() => setEditing(false)}
          onSave={async (input) => {
            await createProject(workspace.id, input);
            await client.invalidateQueries({
              queryKey: projectKey(workspace.id, user.id),
            });
          }}
        />
      )}
    </section>
  );
}

export function ProjectDetailPage() {
  const { principal } = useAuth();
  const { projectId } = useParams();
  if (!principal || !projectId) throw new Error("项目详情缺少身份或项目");
  // 账户或项目切换时卸载编辑状态，不能保留上个项目的凭据输入。
  return (
    <ProjectDetailContent
      key={`${principal.workspace.id}:${principal.user.id}:${projectId}`}
      projectId={projectId}
    />
  );
}

function ProjectDetailContent({ projectId }: { projectId: string }) {
  const { principal } = useAuth();
  if (!principal) throw new Error("缺少身份");
  const { workspace, user } = principal;
  const client = useQueryClient();
  const [projectEditor, setProjectEditor] = useState<Project | null>(null);
  const [repositoryEditor, setRepositoryEditor] = useState<
    Repository | "new" | null
  >(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const codeSources = useQuery({
    queryKey: codeSourceKey(workspace.id, user.id),
    queryFn: () => listCodeSources(workspace.id),
    enabled: repositoryEditor !== null,
  });
  const buildProfiles = useQuery({
    queryKey: [...codeSourceKey(workspace.id, user.id), "profiles"],
    queryFn: () => listBuildProfiles(workspace.id),
    enabled: repositoryEditor !== null,
  });
  const [busy, setBusy] = useState(false);
  const query = useQuery({
    queryKey: projectKey(workspace.id, user.id, projectId),
    queryFn: () => getProject(workspace.id, projectId),
    refetchOnWindowFocus: "always",
    staleTime: 0,
  });
  async function refresh() {
    await client.invalidateQueries({
      queryKey: projectKey(workspace.id, user.id),
    });
  }
  const backLink = <AssetBackLink to="/projects" label="返回项目列表" />;
  if (query.isError)
    return (
      <section className="detail-page asset-mobile-detail">
        {backLink}
        <AssetLoadError
          title="无法访问项目"
          description={errorMessage(query.error)}
          onRetry={() => void query.refetch()}
        />
      </section>
    );
  const project = query.data;
  if (!project)
    return (
      <section className="detail-page asset-mobile-detail">
        {backLink}
        <Typography.Paragraph>正在读取项目资料…</Typography.Paragraph>
      </section>
    );
  return (
    <section className="detail-page asset-mobile-detail page-enter">
      {backLink}
      <header className="page-header">
        <div>
          <span className="page-eyebrow">PROJECT / v{project.version}</span>
          <Typography.Title heading={1}>{project.name}</Typography.Title>
          <Typography.Paragraph>
            {project.description || "未填写业务说明"}
          </Typography.Paragraph>
          <div className="detail-inline-meta">
            <Tag color={project.status === "active" ? "green" : "grey"}>
              {project.status === "active" ? "活动" : "已归档"}
            </Tag>
            <small>资料版本 v{project.version}</small>
          </div>
        </div>
        {project.can_manage && (
          <div className="run-actions">
            <Button onClick={() => setProjectEditor(project)}>
              编辑项目资料
            </Button>
            <Button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setActionError(null);
                try {
                  await editProject(
                    workspace.id,
                    project,
                    project,
                    project.status === "active" ? "archived" : "active",
                  );
                  await refresh();
                } catch (error) {
                  setActionError(errorMessage(error));
                } finally {
                  setBusy(false);
                }
              }}
            >
              {project.status === "active" ? "归档项目" : "恢复项目"}
            </Button>
          </div>
        )}
      </header>
      {actionError && (
        <Banner type="danger" closeIcon={null} description={actionError} />
      )}
      {project.status === "archived" && (
        <Banner
          type="warning"
          closeIcon={null}
          description="项目已归档，不能用于新任务，关联任务资料访问已阻断。"
        />
      )}
      <section className="asset-surface project-overview">
        <Typography.Title heading={4}>项目背景</Typography.Title>
        <dl className="project-facts">
          <dt>整体架构</dt>
          <dd>{project.architecture || "未填写"}</dd>
          <dt>研发约定</dt>
          <dd>{project.conventions || "未填写"}</dd>
        </dl>
      </section>
      <section className="asset-surface project-overview">
        <div className="page-header">
          <div>
            <Typography.Title heading={4}>项目仓库</Typography.Title>
            <Typography.Paragraph type="tertiary">
              任务按需选择仓库，不会自动使用整个项目的仓库或凭据。
            </Typography.Paragraph>
          </div>
          {project.can_manage && project.status === "active" && (
            <Button theme="solid" onClick={() => setRepositoryEditor("new")}>
              添加仓库
            </Button>
          )}
        </div>
        <Banner
          closeIcon={null}
          type="info"
          description="校验连接只检查默认分支的可读性；新任务自动固定提交并准备只读 Git 快照。连接成功不代表执行测试或允许推送。修改地址会清除旧密码或令牌。"
        />
        <div className="project-repositories">
          {project.repositories.map((repository) => (
            <article className="project-repository" key={repository.id}>
              <div className="project-repository-title">
                <Typography.Title heading={5}>
                  {repository.name}
                </Typography.Title>
                <Tag color={repository.status === "active" ? "blue" : "grey"}>
                  {repository.status !== "active"
                    ? "已停用"
                    : repository.connection_status === "connected"
                      ? "连接已验证"
                      : repository.connection_status === "failed"
                        ? "连接失败"
                        : "待校验"}
                </Tag>
              </div>
              <p>{repository.purpose || "未填写用途"}</p>
              <code>{repository.git_url}</code>
              {repository.connection_check && (
                <p>
                  {repository.connection_check.revision
                    ? `校验提交：${repository.connection_check.revision}`
                    : repository.connection_check.message}{" "}
                  {repository.connection_check.checked_at}
                </p>
              )}
              <dl className="project-facts">
                <dt>平台 / 分支</dt>
                <dd>
                  {repository.provider} / {repository.default_branch}
                </dd>
                <dt>技术栈</dt>
                <dd>{repository.technology || "未填写"}</dd>
                <dt>开发说明</dt>
                <dd>{repository.development_notes || "未填写"}</dd>
              </dl>
              {project.can_manage && (
                <div className="run-actions">
                  <span>
                    {repository.has_credential
                      ? "凭据已加密保存"
                      : "未保存凭据"}
                  </span>
                  <Button
                    disabled={
                      busy ||
                      project.status !== "active" ||
                      repository.status !== "active"
                    }
                    aria-label={`校验连接 ${repository.name}`}
                    onClick={async () => {
                      setBusy(true);
                      setActionError(null);
                      try {
                        const result = await checkRepository(
                          workspace.id,
                          projectId,
                          repository,
                        );
                        if (result.status !== "connected")
                          setActionError(result.message ?? "连接校验失败");
                        await refresh();
                      } catch (error) {
                        setActionError(errorMessage(error));
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    校验连接
                  </Button>
                  <Button
                    disabled={project.status !== "active"}
                    aria-label={`编辑仓库 ${repository.name}`}
                    onClick={() => setRepositoryEditor(repository)}
                  >
                    编辑仓库
                  </Button>
                </div>
              )}
            </article>
          ))}
        </div>
        {!project.repositories.length && (
          <Empty
            title="尚未登记仓库"
            description="可以分别添加前端、后端、移动端或接口定义仓库。"
          />
        )}
      </section>
      {projectEditor && (
        <ProjectEditor
          initial={projectEditor}
          onClose={() => setProjectEditor(null)}
          onSave={async (input) => {
            await editProject(workspace.id, projectEditor, input);
            await refresh();
          }}
        />
      )}
      {repositoryEditor && (
        <RepositoryEditor
          current={repositoryEditor === "new" ? undefined : repositoryEditor}
          codeSources={codeSources.data?.items ?? []}
          buildProfiles={buildProfiles.data?.items ?? []}
          onClose={() => setRepositoryEditor(null)}
          onSave={async (input) => {
            if (codeSources.isError || buildProfiles.isError)
              throw new Error("代码源或构建环境读取失败，请刷新后重试");
            await saveRepository(
              workspace.id,
              projectId,
              input,
              repositoryEditor === "new" ? undefined : repositoryEditor,
            );
            await refresh();
          }}
        />
      )}
    </section>
  );
}
