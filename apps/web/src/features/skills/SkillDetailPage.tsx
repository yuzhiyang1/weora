import type { TableColumn } from "../../components/console/data-table";
import { buttonVariants } from "@heroui/styles";
import {
  Archive as IconArchive,
  Download as IconDownload,
  Pencil as IconEdit,
  RefreshCw as IconRefresh,
  Send as IconSend,
  Upload as IconUpload,
} from "lucide-react";
import {
  Banner,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Select,
  Skeleton,
  Table,
  TabPane,
  Tabs,
  Tag,
  TextArea,
  Typography,
} from "../../components/console";
import {
  AssetBackLink,
  AssetLoadError,
} from "../../components/console/asset-controls";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import {
  getSkill,
  listSkillEvents,
  listSkillVersions,
  publishSkill,
  replaceSkillDraft,
  setSkillArchived,
  skillDownloadUrl,
  SkillsApiError,
  type SkillDetail,
  type SkillDraft,
  type SkillEvent,
  type SkillFileSource,
  type SkillVersion,
  updateSkill,
} from "./api";
import { SkillPackageDialog } from "./SkillPackageDialog";
import { SkillFileBrowser } from "./SkillFileBrowser";
import { PackageNotes } from "./SkillPackageNotes";
import { SkillContributions } from "./SkillContributions";
import { SkillUsagePanel } from "./SkillUsagePanel";
import { formatDate, SkillLogo, StatusBadge } from "./SkillsPage";
import { useDiscardChanges } from "../settings/useDiscardChanges";

type DetailTab = "files" | "versions" | "activity" | "usage" | "contributions";

export function SkillDetailPage() {
  const { principal } = useAuth();
  const { skillId } = useParams();
  if (!principal || !skillId)
    throw new Error("Skill 详情缺少工作空间或资产标识");
  // 切换资产或账号时丢弃旧弹窗状态，避免把文件或版本号提交到另一个 Skill。
  return (
    <SkillDetailContent
      key={`${principal.workspace.id}:${principal.user.id}:${skillId}`}
      workspaceId={principal.workspace.id}
      skillId={skillId}
      canWrite={["owner", "admin"].includes(principal.workspace.role)}
    />
  );
}

function SkillDetailContent({
  workspaceId,
  skillId,
  canWrite,
}: {
  workspaceId: string;
  skillId: string;
  canWrite: boolean;
}) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<DetailTab>("files");
  const [fileSelection, setFileSelection] = useState("");
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);

  const detailQuery = useQuery({
    queryKey: ["skill", workspaceId, skillId],
    queryFn: () => getSkill(workspaceId, skillId),
  });
  const versionsQuery = useQuery({
    queryKey: ["skill-versions", workspaceId, skillId],
    queryFn: () => listSkillVersions(workspaceId, skillId),
  });
  const eventsQuery = useQuery({
    queryKey: ["skill-events", workspaceId, skillId],
    queryFn: () => listSkillEvents(workspaceId, skillId),
  });
  const archiveMutation = useMutation({
    mutationFn: (archived: boolean) =>
      setSkillArchived(workspaceId, skillId, archived),
    onSuccess: async () =>
      invalidateSkillQueries(queryClient, workspaceId, skillId),
  });
  const backLink = <AssetBackLink to="/skills" label="返回 Skills" />;

  if (detailQuery.isPending) {
    return (
      <section className="detail-page asset-mobile-detail">
        {backLink}
        <Skeleton
          active
          placeholder={<Skeleton.Title style={{ width: "45%" }} />}
        />
      </section>
    );
  }
  if (detailQuery.isError) {
    return (
      <section className="detail-page asset-mobile-detail">
        {backLink}
        <AssetLoadError
          description={errorMessage(detailQuery.error)}
          title="无法打开这个 Skill"
          onRetry={() => void detailQuery.refetch()}
        />
      </section>
    );
  }

  const skill = detailQuery.data;
  return (
    <section className="detail-page asset-mobile-detail page-enter">
      {backLink}
      <header className="detail-hero">
        <div className="detail-title-block">
          <SkillLogo large />
          <div>
            <Typography.Title heading={1}>
              {skill.display_name}
            </Typography.Title>
            {skill.display_name !== skill.slug && <code className="skill-detail-slug">{skill.slug}</code>}
            <Typography.Paragraph>{skill.description}</Typography.Paragraph>
            <div className="detail-inline-meta">
              <StatusBadge status={skill.status} />
              <Tag color={skill.latest_published_version ? "blue" : "orange"}>
                {skill.latest_published_version ? `v${skill.latest_published_version}` : "待发布"}
              </Tag>
              <span>上传人 {skill.uploader_name || "未知"}</span>
              <time dateTime={skill.updated_at}>{formatDate(skill.updated_at)}</time>
            </div>
          </div>
        </div>
        {canWrite ? (
          <div className="detail-actions">
            <Button icon={<IconEdit />} onClick={() => setEditOpen(true)}>
              编辑信息
            </Button>
            <Button
              icon={
                skill.status === "active" ? <IconArchive /> : <IconRefresh />
              }
              loading={archiveMutation.isPending}
              onClick={() => archiveMutation.mutate(skill.status === "active")}
            >
              {skill.status === "active" ? "归档" : "恢复"}
            </Button>
            {skill.status === "active" ? (
              <Button
                icon={<IconUpload />}
                onClick={() => setReplaceOpen(true)}
                theme={skill.draft ? "light" : "solid"}
              >
                {skill.draft ? "替换草稿" : "上传新草稿"}
              </Button>
            ) : null}
            {skill.status === "active" && skill.draft ? (
              <Button
                aria-label="发布版本"
                icon={<IconSend />}
                onClick={() => setPublishOpen(true)}
                theme="solid"
                type="primary"
              >
                发布版本
              </Button>
            ) : null}
          </div>
        ) : null}
      </header>

      {archiveMutation.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(archiveMutation.error)}
          type="danger"
        />
      ) : null}

      <Tabs
        activeKey={tab}
        className="detail-tabs"
        onChange={(key) => {
          setTab(key as DetailTab);
          // 包下载走浏览器原生链接，打开活动页时重新读取刚写入的记录。
          if (key === "activity") void eventsQuery.refetch();
        }}
      >
        <TabPane itemKey="files" tab="文件">
          <FilesTab workspaceId={workspaceId} skill={skill} versions={versionsQuery.data?.items ?? []}
            loading={versionsQuery.isPending} error={versionsQuery.error} canWrite={canWrite}
            selection={fileSelection} onSelectionChange={setFileSelection}
            onRetry={() => void versionsQuery.refetch()} />
        </TabPane>
        <TabPane
          itemKey="versions"
          tab={
            versionsQuery.data
              ? `版本 ${versionsQuery.data.items.length}`
              : "版本"
          }
        >
          <VersionsTab
            error={versionsQuery.error}
            loading={versionsQuery.isPending}
            skillId={skillId}
            versions={versionsQuery.data?.items ?? []}
            workspaceId={workspaceId}
          />
        </TabPane>
        <TabPane itemKey="activity" tab="活动">
          <ActivityTab
            error={eventsQuery.error}
            events={eventsQuery.data?.items ?? []}
            loading={eventsQuery.isPending}
          />
        </TabPane>
        <TabPane itemKey="usage" tab="使用统计">
          {tab === "usage" && <SkillUsagePanel workspaceId={workspaceId} skillId={skillId}
            versions={versionsQuery.data?.items ?? []} canWrite={canWrite} />}
        </TabPane>
        {canWrite && <TabPane itemKey="contributions" tab="待审核">
          {tab === "contributions" && <SkillContributions workspaceId={workspaceId} skillId={skillId}
            archived={skill.status === "archived"} hasDraft={!!skill.draft} onChanged={async (accept) => {
              await invalidateSkillQueries(queryClient, workspaceId, skillId);
              if (accept) {
                setFileSelection("draft");
                setTab("files");
              }
            }} />}
        </TabPane>}
      </Tabs>

      <SkillPackageDialog
        onClose={() => setReplaceOpen(false)}
        onSubmit={async (file) => {
          await replaceSkillDraft(workspaceId, skillId, file);
          await invalidateSkillQueries(queryClient, workspaceId, skillId);
          setReplaceOpen(false);
          setFileSelection("draft");
          setTab("files");
        }}
        open={replaceOpen}
        submitLabel="校验并替换"
        title={skill.draft ? "替换当前草稿" : "上传新草稿"}
      />
      {canWrite && publishOpen && skill.draft ? (
        <PublishDialog
          draft={skill.draft}
          initialVersion={nextPatchVersion(skill.latest_published_version)}
          onClose={() => setPublishOpen(false)}
          onPublish={async (input) => {
            await publishSkill(workspaceId, skillId, input);
            await invalidateSkillQueries(queryClient, workspaceId, skillId);
            setPublishOpen(false);
            setTab("versions");
          }}
        />
      ) : null}
      {canWrite && editOpen && (
        <EditSkillDialog
          onClose={() => setEditOpen(false)}
          onSave={async (input) => {
            await updateSkill(workspaceId, skillId, input);
            await invalidateSkillQueries(queryClient, workspaceId, skillId);
            setEditOpen(false);
          }}
          skill={skill}
        />
      )}
    </section>
  );
}

/** 默认打开发布版本，管理员可切换到当前草稿查看待发布内容。 */
function FilesTab({ workspaceId, skill, versions, loading, error, canWrite, selection, onSelectionChange, onRetry }: {
  workspaceId: string;
  skill: SkillDetail;
  versions: SkillVersion[];
  loading: boolean;
  error: Error | null;
  canWrite: boolean;
  selection: string;
  onSelectionChange: (value: string) => void;
  onRetry: () => void;
}) {
  const draft = canWrite ? skill.draft : null;
  const options = [
    ...versions.map((version) => ({ label: `已发布 ${version.version}`, value: version.version })),
    ...(draft ? [{ label: "当前草稿", value: "draft" }] : []),
  ];
  const selected = options.some((option) => option.value === selection)
    ? selection : options[0]?.value;
  const record = selected === "draft" ? draft : versions.find((item) => item.version === selected);
  if (error) return <AssetLoadError title="版本暂时无法加载" description={errorMessage(error)} onRetry={onRetry} />;
  if (loading) return <Skeleton active />;
  if (!record || !selected) return <Card className="detail-card">
    <Empty title="暂无可查看的文件" description="管理员发布版本后，团队成员即可浏览包内容。" />
  </Card>;

  const source: SkillFileSource = selected === "draft"
    ? { draft: record.package_sha256 } : { version: selected };
  return <div className="skill-content-panel">
    <div className="skill-package-bar">
      <h2>包内容</h2>
      {record.uploader_name && record.uploader_name !== skill.uploader_name &&
        <span className="skill-uploader">此包上传人 {record.uploader_name}</span>}
      <Select aria-label="包版本" value={selected} optionList={options}
        onChange={(value) => onSelectionChange(String(value ?? ""))} />
      {selected !== "draft" && <a className={buttonVariants({ variant: "outline", size: "sm" })}
        href={skillDownloadUrl(workspaceId, skill.id, selected)}><IconDownload />下载包</a>}
    </div>
    <SkillFileBrowser key={`${selected}:${record.package_sha256}`} workspaceId={workspaceId}
      skillId={skill.id} source={source} files={record.files} />
    <PackageNotes key={record.package_sha256} record={record} />
  </div>;
}

function VersionsTab({
  error,
  loading,
  versions,
  workspaceId,
  skillId,
}: {
  error: Error | null;
  loading: boolean;
  versions: SkillVersion[];
  workspaceId: string;
  skillId: string;
}) {
  const columns: TableColumn<SkillVersion>[] = [
    {
      key: "version",
      title: "版本",
      dataIndex: "version",
      width: 130,
      render: (value: string, _record: SkillVersion, index: number) => (
        <span className="version-cell">
          <Tag color="blue">v{value}</Tag>
          {index === 0 ? <Tag color="green">Latest</Tag> : null}
        </span>
      ),
    },
    {
      key: "status",
      title: "状态",
      dataIndex: "id",
      width: 120,
      render: () => <Tag color="green">已发布</Tag>,
    },
    { key: "changelog", title: "变更说明", dataIndex: "changelog" },
    { title: "上传人", dataIndex: "uploader_name", width: 116,
      render: (value: string | null) => value || "未知" },
    {
      key: "published_at",
      title: "发布时间",
      dataIndex: "published_at",
      width: 170,
      render: (value: string) => formatDate(value),
    },
    {
      key: "download",
      title: "操作",
      dataIndex: "version",
      width: 100,
      render: (value: string) => (
        <a
          className={buttonVariants({ variant: "ghost", size: "sm" })}
          href={skillDownloadUrl(workspaceId, skillId, value)}
        >
          <IconDownload />
          下载
        </a>
      ),
    },
  ];
  if (error)
    return (
      <Banner
        closeIcon={null}
        description={errorMessage(error)}
        type="danger"
      />
    );
  return (
    <Card className="detail-card">
      {(loading || versions.length > 0) && (
        <Table
          className="asset-stack-table"
          variant="secondary"
          columns={columns}
          dataSource={versions}
          loading={loading}
          pagination={false}
          rowKey="id"
        />
      )}
      {!loading && !versions.length ? (
        <div className="picker-empty">还没有发布版本。</div>
      ) : null}
    </Card>
  );
}

function ActivityTab({
  loading,
  error,
  events,
}: {
  loading: boolean;
  error: Error | null;
  events: SkillEvent[];
}) {
  if (loading) return <Skeleton active />;
  if (error)
    return (
      <Banner
        closeIcon={null}
        description={errorMessage(error)}
        type="danger"
      />
    );
  return (
    <Card className="detail-card">
      <div className="activity-list">
        {events.length ? (
          events.map((event) => (
            <div className="activity-row" key={event.id}>
              <span className="activity-dot" />
              <div>
                <strong>{eventLabel(event.event_type)}</strong>
                <small>
                  {event.event_type === "SKILL_DOWNLOAD_REQUESTED" &&
                    `下载人 ${event.actor_name || "未知"} · `}
                  {formatDate(event.occurred_at)} · Request{" "}
                  {event.request_id.slice(0, 12)}
                </small>
              </div>
              {typeof event.data.version === "string" ? (
                <Tag color="blue">v{event.data.version}</Tag>
              ) : null}
            </div>
          ))
        ) : (
          <div className="picker-empty">还没有活动记录。</div>
        )}
      </div>
    </Card>
  );
}

function PublishDialog({
  draft,
  initialVersion,
  onClose,
  onPublish,
}: {
  draft: SkillDraft;
  initialVersion: string;
  onClose: () => void;
  onPublish: (input: { version: string; changelog: string; draft_sha256: string }) => Promise<void>;
}) {
  const [version, setVersion] = useState(initialVersion);
  const [changelog, setChangelog] = useState("");
  // 对话框打开时固定预览的包摘要，后台刷新草稿时仍要求重新确认。
  const [reviewedDraft] = useState(draft);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const blocked = reviewedDraft.warnings.some((item) => item.blocks_publish);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting || blocked || !version.trim() || !changelog.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await onPublish({
        version: version.trim(), changelog: changelog.trim(), draft_sha256: reviewedDraft.package_sha256,
      });
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
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
      title="发布 Skill"
      visible
      width={580}
    >
      <form className="skill-form" onSubmit={submit}>
        <label className="form-field">
          <span>版本号</span>
          <Input
            aria-label="版本号"
            onChange={setVersion}
            placeholder="0.1.0"
            value={version}
          />
        </label>
        <label className="form-field">
          <span>变更说明</span>
          <TextArea
            aria-label="变更说明"
            maxLength={2000}
            onChange={setChangelog}
            placeholder="说明这个版本新增或调整了什么"
            rows={4}
            value={changelog}
          />
        </label>
        <div className="publish-summary">
          <span>
            <small>内容校验值</small>
            <code>{shortHash(reviewedDraft.content_sha256)}</code>
          </span>
          <span>
            <small>文件</small>
            <strong>{reviewedDraft.files.length} 个</strong>
          </span>
        </div>
        {blocked ? (
          <Banner
            closeIcon={null}
            description="当前草稿包含阻断风险，请移除私钥等敏感内容后重新上传。"
            type="danger"
          />
        ) : null}
        {error ? (
          <Banner closeIcon={null} description={error} type="danger" />
        ) : null}
        <div className="modal-actions">
          <Button disabled={submitting} onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={blocked || !version.trim() || !changelog.trim()}
            htmlType="submit"
            loading={submitting}
            theme="solid"
            type="primary"
          >
            发布不可变版本
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EditSkillDialog({
  skill,
  onClose,
  onSave,
}: {
  skill: SkillDetail;
  onClose: () => void;
  onSave: (input: {
    display_name: string;
    description: string;
  }) => Promise<void>;
}) {
  const [displayName, setDisplayName] = useState(skill.display_name);
  const [description, setDescription] = useState(skill.description);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { requestClose, confirmation } = useDiscardChanges(
    displayName !== skill.display_name || description !== skill.description,
    onClose,
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSave({
        display_name: displayName.trim(),
        description: description.trim(),
      });
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <>
      <Modal
        centered
        closeOnEsc={!submitting}
        maskClosable={!submitting}
        footer={null}
        onCancel={requestClose}
        title="编辑 Skill 信息"
        visible
        width={560}
      >
        <form className="skill-form" onSubmit={submit}>
          <label className="form-field">
            <span>显示名称</span>
            <Input
              aria-label="显示名称"
              maxLength={120}
              onChange={setDisplayName}
              value={displayName}
            />
          </label>
          <label className="form-field">
            <span>描述</span>
            <TextArea
              aria-label="描述"
              maxLength={2000}
              onChange={setDescription}
              rows={4}
              value={description}
            />
          </label>
          <Typography.Paragraph type="tertiary">
            这里只修改工作空间展示信息，不改变包内 SKILL.md 或历史版本。
          </Typography.Paragraph>
          {error ? (
            <Banner closeIcon={null} description={error} type="danger" />
          ) : null}
          <div className="modal-actions">
            <Button disabled={submitting} onClick={requestClose}>
              取消
            </Button>
            <Button
              disabled={!displayName.trim() || !description.trim()}
              htmlType="submit"
              loading={submitting}
              theme="solid"
              type="primary"
            >
              保存
            </Button>
          </div>
        </form>
      </Modal>
      {confirmation}
    </>
  );
}

async function invalidateSkillQueries(
  queryClient: ReturnType<typeof useQueryClient>,
  workspaceId: string,
  skillId: string,
) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["skills", workspaceId] }),
    queryClient.invalidateQueries({
      queryKey: ["skill", workspaceId, skillId],
    }),
    queryClient.invalidateQueries({
      queryKey: ["skill-versions", workspaceId, skillId],
    }),
    queryClient.invalidateQueries({
      queryKey: ["skill-events", workspaceId, skillId],
    }),
  ]);
}

function nextPatchVersion(current: string | null): string {
  if (!current) return "0.1.0";
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(current);
  return match ? `${match[1]}.${match[2]}.${Number(match[3]) + 1}` : "0.1.0";
}

function eventLabel(type: string): string {
  return (
    (
      {
        SKILL_CONTRIBUTION_ADDED: "提交候选包",
        SKILL_CONTRIBUTION_ACCEPTED: "接受候选包",
        SKILL_CONTRIBUTION_REJECTED: "驳回候选包",
        SKILL_CREATED: "创建 Skill",
        SKILL_DRAFT_REPLACED: "替换草稿",
        SKILL_PUBLISHED: "发布版本",
        SKILL_METADATA_UPDATED: "更新展示信息",
        SKILL_ARCHIVED: "归档 Skill",
        SKILL_RESTORED: "恢复 Skill",
        SKILL_DOWNLOAD_REQUESTED: "下载发布包",
      } as Record<string, string>
    )[type] ?? type
  );
}

function errorMessage(error: unknown): string {
  return error instanceof SkillsApiError
    ? error.message
    : "操作未完成，请稍后重试。";
}

function shortHash(value: string): string {
  return `${value.slice(0, 10)}…${value.slice(-6)}`;
}
