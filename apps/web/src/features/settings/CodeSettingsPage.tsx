import { RepositorySettingsTable } from "./RepositorySettingsTable";
import { GitPreferencesForm } from "./GitSettingsCard";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Plus, ShieldCheck } from "lucide-react";
import {
  Banner,
  Button,
  Card,
  Empty,
  Modal,
  Tag,
} from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import {
  checkRepository,
  getProject,
  projectKey,
  saveRepository,
  type Repository,
} from "../projects/api";
import {
  codeSourceKey,
  importLegacyCodeSource,
  listBuildProfiles,
  listCodeSources,
  listLegacyCodeSources,
  saveCodeSource,
  type CodeSource,
  type LegacyCodeSource,
} from "../projects/codeSourcesApi";
import { CodeSourceEditor } from "../projects/CodeSettingsPage";
import { RepositoryEditor } from "../projects/ProjectEditors";
import { useSettingsEnvironment } from "./SettingsEnvironment";
import {
  SettingsLoading,
  SettingsPage,
  SettingsResult,
  useTeamManager,
} from "./SettingsPage";

export function CodeSettingsPage() {
  const { principal } = useAuth();
  const workspace = principal!.workspace.id,
    user = principal!.user.id;
  const manager = useTeamManager();
  const env = useSettingsEnvironment()!;
  const cache = useQueryClient();
  const sources = useQuery({
    queryKey: codeSourceKey(workspace, user),
    queryFn: () => listCodeSources(workspace),
  });
  const profiles = useQuery({
    queryKey: ["build-profiles", workspace, user],
    queryFn: () => listBuildProfiles(workspace),
  });
  const legacy = useQuery({
    queryKey: ["legacy-code-sources", workspace, user],
    queryFn: () => listLegacyCodeSources(workspace),
    enabled: manager,
  });
  const project = useQuery({
    queryKey: projectKey(workspace, user, env.project?.id ?? ""),
    queryFn: () => getProject(workspace, env.project!.id),
    enabled: !!env.project,
  });
  const [source, setSource] = useState<CodeSource | "new" | null>(null);
  const [repo, setRepo] = useState<Repository | "new" | null>(null);
  const [candidate, setCandidate] = useState<LegacyCodeSource | null>(null);
  const [checking, setChecking] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const refresh = async () => {
    await Promise.all([
      cache.invalidateQueries({ queryKey: codeSourceKey(workspace, user) }),
      cache.invalidateQueries({ queryKey: projectKey(workspace, user) }),
      cache.invalidateQueries({
        queryKey: ["legacy-code-sources", workspace, user],
      }),
    ]);
  };
  async function checkConnection(repository: Repository) {
    if (checking) return;
    setChecking(repository.id);
    setError(null);
    setSuccess(null);
    try {
      const result = await checkRepository(
        workspace,
        repository.project_id,
        repository,
      );
      if (result.status !== "connected")
        throw new Error(result.message || "仓库读取检测失败");
      setSuccess("仓库读取检测通过；写入和推送仍需任务审批。");
    } catch (cause) {
      setError(cause);
    } finally {
      try {
        await refresh();
      } finally {
        setChecking(null);
      }
    }
  }
  return (
    <SettingsPage
      title="代码"
      description="团队维护共享连接；关联仓库只作用于当前业务空间。"
      scope="混合范围"
    >
      <div className="settings-stack">
        <GitPreferencesForm />
        <Card
          title="代码托管连接"
          headerExtraContent={
            <div className="settings-inline">
              <Tag color="blue">团队级</Tag>
              {manager && (
                <Button
                  icon={<Plus size={16} />}
                  onClick={() => setSource("new")}
                >
                  添加连接
                </Button>
              )}
            </div>
          }
        >
          <p className="form-help">
            连接在团队内共享，业务空间的仓库可按需关联。API 令牌与 Git
            读写凭据独立管理。
          </p>
          <SettingsLoading
            loading={sources.isPending}
            error={sources.error}
            retry={sources.refetch}
          />
          {sources.data?.items.length === 0 && (
            <p className="form-help">
              尚未添加代码托管连接。普通 Git URL 可直接添加；需要云效 MR
              能力时，再配置 Codeup 连接。
            </p>
          )}
          <div className="settings-list">
            {!sources.isError &&
              sources.data?.items.map((item) => (
                <div className="settings-list-row" key={item.id}>
                  <div className="settings-list-copy">
                    <strong>{item.name}</strong>
                    <p>
                      {item.provider} · {item.organization}
                    </p>
                  </div>
                  <Tag>
                    {item.enabled
                      ? item.has_credential
                        ? "已配置 · 未检测"
                        : "未配置凭据"
                      : "已停用"}
                  </Tag>
                  {item.can_manage && (
                    <Button onClick={() => setSource(item)}>配置</Button>
                  )}
                </div>
              ))}
          </div>
          {manager &&
            legacy.data?.items.map((item) => (
              <div className="settings-list-row" key={item.fingerprint}>
                <p className="form-help">
                  已有部署连接可导入：{item.repository_name}
                </p>
                <Button onClick={() => setCandidate(item)}>导入存量配置</Button>
              </div>
            ))}
          {legacy.error && <SettingsResult error={legacy.error} />}
        </Card>
        <Card
          title={
            <span className="settings-inline">
              关联仓库<Tag>{env.project?.name ?? "当前业务空间"}</Tag>
            </span>
          }
          headerExtraContent={
            env.project?.can_manage && (
              <Button
                icon={<Plus size={16} />}
                theme="solid"
                disabled={
                  sources.isPending ||
                  profiles.isPending ||
                  !!sources.error ||
                  !!profiles.error
                }
                onClick={() => setRepo("new")}
              >
                添加仓库
              </Button>
            )
          }
        >
          <SettingsLoading
            loading={env.loading || (!!env.project && project.isPending)}
            error={env.error || project.error}
            retry={async () => {
              await env.refresh();
              await project.refetch();
            }}
          />
          {!env.project && !env.loading && !env.error && (
            <Empty
              title="请先创建业务空间"
              description="仓库归属于业务空间；一条业务线可添加多个前后端或 App 仓库。"
            >
              <Button onClick={env.create}>创建业务空间</Button>
            </Empty>
          )}
          {env.project &&
            !env.error &&
            !project.isError &&
            project.data?.repositories.length === 0 && (
              <Empty
                title="当前业务空间还没有仓库"
                description="添加 Git URL 后，可显式检测读取连接，再用于任务上下文。"
              />
            )}
          {!env.error &&
            !project.isError &&
            !!project.data?.repositories.length && (
              <RepositorySettingsTable
                repositories={project.data.repositories}
                canManage={!!env.project?.can_manage}
                checking={checking}
                onCheck={checkConnection}
                onEdit={setRepo}
              />
            )}
          <p className="form-help">
            仅展示当前业务空间的仓库。
            <Link to="/settings/business-space">管理业务空间资料 →</Link>
          </p>
        </Card>
        {(profiles.error || sources.error) && (
          <Banner
            type="warning"
            description="配置选项加载失败，暂不可新增仓库。请重新加载后重试。"
          />
        )}
        <SettingsResult error={error} success={success} />
        <p className="settings-footnote">
          <ShieldCheck size={16} />
          新增连接和仓库不会授予执行权限。修改文件、执行命令、提交和推送继续遵循现有任务授权与审批。
        </p>
      </div>
      {source && (
        <CodeSourceEditor
          current={source === "new" ? undefined : source}
          onClose={() => setSource(null)}
          onSave={async (input) => {
            await saveCodeSource(
              workspace,
              input,
              source === "new" ? undefined : source,
            );
            await refresh();
          }}
        />
      )}
      {repo && env.project && (
        <RepositoryEditor
          key={`${env.project.id}:${repo === "new" ? "new" : repo.id}`}
          current={repo === "new" ? undefined : repo}
          codeSources={sources.data?.items ?? []}
          buildProfiles={profiles.data?.items ?? []}
          onClose={() => setRepo(null)}
          onSave={async (input) => {
            await saveRepository(
              workspace,
              env.project!.id,
              input,
              repo === "new" ? undefined : repo,
            );
            await refresh();
          }}
        />
      )}
      <Modal
        visible={!!candidate}
        title="导入已有团队连接？"
        onCancel={() => {
          if (!importing) setCandidate(null);
        }}
        closeOnEsc={!importing}
        maskClosable={!importing}
        footer={null}
      >
        <p className="form-help">
          将后端已有的云效连接登记到控制台，不向浏览器返回令牌，也不验证远端权限。
        </p>
        <div className="modal-actions">
          <Button disabled={importing} onClick={() => setCandidate(null)}>
            取消
          </Button>
          <Button
            theme="solid"
            loading={importing}
            onClick={async () => {
              if (!candidate || importing) return;
              setImporting(true);
              try {
                await importLegacyCodeSource(workspace, candidate);
                await refresh();
                setCandidate(null);
              } catch (cause) {
                setError(cause);
              } finally {
                setImporting(false);
              }
            }}
          >
            确认导入
          </Button>
        </div>
      </Modal>
    </SettingsPage>
  );
}
