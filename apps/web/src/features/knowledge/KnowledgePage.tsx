import { buttonVariants } from "@heroui/styles";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  Banner, Button, Card, Input, Modal, Select, Skeleton, TabPane, Tabs, TextArea, Typography,
} from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { useAuth } from "../auth/AuthProvider";
import { listProjects, projectKey } from "../projects/api";
import { errorMessage } from "../runs/api";
import { skillPagePath } from "../skills/api";
import { contribute, detail, feedback, health, knowledgeTypes, list, review, type Entry, type Scope } from "./api";
import { KnowledgeHealthReport } from "./KnowledgeHealthReport";
import { KnowledgeMaintenance } from "./KnowledgeMaintenance";
import { KnowledgeRows } from "./KnowledgeRows";
import { KnowledgeSources } from "./KnowledgeSources";
import { KnowledgeWorkflowSettings } from "./KnowledgeWorkflowSettings";
import "./knowledge.css";

const types: Record<string, string> = knowledgeTypes;
const tabs = ["search", "review", "maintenance", "sources", "workflows", "health"];
const reviewerTabs = ["review", "health"];
const kindOptions = [
  { value: "all", label: "全部类型" },
  ...Object.entries(types).map(([value, label]) => ({ value, label })),
];

export function KnowledgePage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("知识库缺少当前身份");

  const { workspace, user } = principal;
  const [params, setParams] = useSearchParams();
  const project = params.get("project") ?? "";
  const selectedScope = project || "shared";
  const [editing, setEditing] = useState(false);
  const projects = useQuery({
    queryKey: projectKey(workspace.id, user.id), queryFn: () => listProjects(workspace.id),
  });
  const activeProjects = projects.data?.items.filter((item) => item.status === "active") ?? [];
  const options = [
    { value: "shared", label: "工作空间共享知识" },
    ...activeProjects.map(({ id, name }) => ({ value: id, label: name })),
  ];

  // 直接访问链接时也应用页签权限，避免发起当前角色无法使用的查询。
  const reviewer = ["owner", "admin"].includes(workspace.role);
  const requestedTab = params.get("view") ?? "search";
  const canOpenTab = tabs.includes(requestedTab) && (reviewer || !reviewerTabs.includes(requestedTab));
  const tab = canOpenTab ? requestedTab : "search";

  function updateLocation(name: "project" | "view", value?: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  }

  return <section className="asset-page page-enter knowledge-workbench">
    <header className="page-header knowledge-workbench-header">
      <div className="knowledge-heading">
        <Typography.Title heading={1}>项目知识库</Typography.Title>
        <p className="knowledge-meta">检索经验和代码证据，管理团队知识的发布。</p>
      </div>
      <div className="knowledge-header-actions">
        <div className="knowledge-scope">
          <Select aria-label="知识范围" optionList={options} value={selectedScope} onChange={(value) => {
            setEditing(false);
            updateLocation("project", value === "shared" ? undefined : String(value));
          }} />
        </div>
        <Button theme="solid" onClick={() => setEditing(true)}>贡献经验</Button>
      </div>
    </header>
    {projects.isError && <Button onClick={() => void projects.refetch()}>重试加载项目</Button>}
    {/* 按当前身份和项目重建内容区，切换范围时清空旧条目的详情和草稿。 */}
    <KnowledgePanel key={`${workspace.id}:${user.id}:${project}`} scope={{ workspace: workspace.id, project }}
      scopeLabel={options.find((option) => option.value === selectedScope)?.label ?? project}
      user={user.id} reviewer={reviewer} tab={tab}
      setTab={(value) => updateLocation("view", value === "search" ? undefined : value)}
      editing={editing} closeEditing={() => setEditing(false)} />
  </section>;
}

function KnowledgePanel({ scope, scopeLabel, user, reviewer, tab, setTab, editing, closeEditing }: {
  scope: Scope; scopeLabel: string; user: string; reviewer: boolean;
  tab: string; setTab: (value: string) => void; editing: boolean; closeEditing: () => void;
}) {
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [kind, setKind] = useState("");
  const [githubOnly, setGithubOnly] = useState(false);
  const [selected, setSelected] = useState<Entry | null>(null);
  const cache = useQueryClient();
  const key = ["knowledge", scope.workspace, user, scope.project];
  const isReview = tab === "review";
  const rows = useQuery({
    queryKey: [...key, tab, searchQuery, kind, githubOnly],
    queryFn: () => list(scope, isReview, searchQuery, kind, githubOnly),
    enabled: tab === "search" || isReview,
  });
  const report = useQuery({ queryKey: [...key, "health"], queryFn: () => health(scope), enabled: tab === "health" });
  const refresh = () => cache.invalidateQueries({ queryKey: key });

  // 审核针对点选时的版本。刷新后版本发生变化时，先让审核人重新选择。
  const pending = selected && rows.data?.items.find((item) =>
    item.id === selected.id && item.revision === selected.revision,
  );
  const results = rows.isPending ? <Skeleton active /> : rows.isError ? (
    <AssetLoadError title="知识暂时无法加载" description={errorMessage(rows.error)} onRetry={() => void rows.refetch()} />
  ) : <KnowledgeRows title={isReview ? "待审核版本" : "知识条目"}
    empty={isReview ? "没有待审核版本" : "没有匹配的知识"}
    rows={rows.data.items.map((entry) => ({
      id: entry.id,
      title: entry.title,
      meta: [types[entry.kind] ?? entry.kind, isReview && `版本 ${entry.revision}`,
        entry.project_id ? "当前项目" : "工作空间共享", sourceLabel(entry)].filter(Boolean).join(" · "),
      onOpen: () => setSelected(entry),
    }))} />;

  function openGithubReviews() {
    // 来源入口展示所有 GitHub 评审经验，先清除上一次检索留下的关键词和类型。
    setSearchInput("");
    setSearchQuery("");
    setKind("");
    setGithubOnly(true);
    setSelected(null);
    setTab("search");
  }

  return <>
    <Tabs className="knowledge-tabs" activeKey={tab} onChange={(value) => { setSelected(null); setTab(value); }}>
      <TabPane itemKey="search" tab="检索">
        <form className="knowledge-searchbar" onSubmit={(event) => {
          event.preventDefault();
          setSearchQuery(searchInput.trim());
        }}>
          <Input className="knowledge-search" aria-label="搜索知识" value={searchInput} placeholder="输入标题、关键词或代码符号"
            maxLength={512} onChange={setSearchInput} />
          <div className="knowledge-kind"><Select aria-label="知识类型" value={kind || "all"}
            optionList={kindOptions}
            onChange={(value) => setKind(value === "all" ? "" : String(value))} /></div>
          <Button htmlType="submit" theme="solid">搜索</Button>
        </form>
        {githubOnly && <div className="knowledge-actions"><span>来源 · GitHub 评审</span>
          <Button size="small" onClick={() => setGithubOnly(false)}>清除来源筛选</Button></div>}
        {results}
      </TabPane>
      {reviewer && <TabPane itemKey="review" tab="待审核">
        <div className="knowledge-stack">{results}
          {pending && <ReviewPanel
            key={`${pending.id}:${pending.revision}`} scope={scope} entry={pending}
            onClose={() => setSelected(null)} refresh={refresh} />}
        </div>
      </TabPane>}
      <TabPane itemKey="maintenance" tab="维护">
        {tab === "maintenance" && <KnowledgeMaintenance scope={scope} user={user} reviewer={reviewer} refresh={refresh} />}
      </TabPane>
      <TabPane itemKey="sources" tab="来源">
        {tab === "sources" && <KnowledgeSources scope={scope} user={user} onReviews={openGithubReviews} />}
      </TabPane>
      <TabPane itemKey="workflows" tab="工作流">
        {tab === "workflows" && <KnowledgeWorkflowSettings scope={scope} user={user} reviewer={reviewer} />}
      </TabPane>
      {reviewer && <TabPane itemKey="health" tab="健康报告">
        {report.isPending ? <Skeleton active /> : report.isError ? (
          <AssetLoadError title="报告暂时无法加载" description={errorMessage(report.error)}
            onRetry={() => void report.refetch()} />
        ) : <KnowledgeHealthReport report={report.data} labels={types} />}
      </TabPane>}
    </Tabs>
    {editing && <ContributionDialog scope={scope} scopeLabel={scopeLabel} onClose={closeEditing} refresh={refresh} />}
    {selected && tab === "search" && <EntryDialog scope={scope} entry={selected}
      key={selected.id} user={user} onClose={() => setSelected(null)} />}
  </>;
}

type DialogProps = { scope: Scope; onClose: () => void };
type KnowledgeWriteProps = DialogProps & { refresh: () => Promise<void> };

function ContributionDialog({ scope, scopeLabel, onClose, refresh }: KnowledgeWriteProps & { scopeLabel: string }) {
  // 同一份草稿重试时沿用提交 ID，防止服务端已写入但响应丢失时重复发布。
  const [operationId] = useState(() => crypto.randomUUID());
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [ref, setRef] = useState("");
  const publish = useMutation({
    mutationFn: () => contribute(scope, { operation_id: operationId, title: title.trim(), body: body.trim(), ref }),
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  return <Modal visible title="贡献经验" onCancel={onClose} maskClosable={!publish.isPending} closeOnEsc={!publish.isPending}>
    <form className="knowledge-form" onSubmit={(event) => { event.preventDefault(); publish.mutate(); }}>
      <p className="knowledge-meta">发布到 {scopeLabel}</p>
      <label>标题<Input maxLength={160} value={title} onChange={setTitle} required disabled={publish.isPending} /></label>
      <label>经验正文<TextArea maxLength={64000} value={body} onChange={setBody} rows={8}
        required disabled={publish.isPending} /></label>
      <label>出处<Input maxLength={1000} value={ref} onChange={setRef} disabled={publish.isPending} /></label>
      {publish.isError && <Banner closeIcon={null} type="danger" description={errorMessage(publish.error)} />}
      <div className="knowledge-actions">
        <Button onClick={onClose} disabled={publish.isPending}>取消</Button>
        <Button htmlType="submit" theme="solid" disabled={!title.trim() || !body.trim()} loading={publish.isPending}>
          发布经验
        </Button>
      </div>
    </form>
  </Modal>;
}

function sourceLabel(entry: Entry) {
  const reviewNumber = entry.source.type === "pull_request" &&
    /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/(\d+)$/.exec(entry.source.ref);
  return reviewNumber ? `GitHub PR #${reviewNumber[1]}` : "";
}

function EntryContent({ entry, linkSkill = false }: { entry: Entry; linkSkill?: boolean }) {
  const github = sourceLabel(entry);
  return <div className="knowledge-form">
    <p className="knowledge-meta">{types[entry.kind] ?? entry.kind} · 版本 {entry.revision}</p>
    {entry.source.ref && <p className="knowledge-text">出处 · {github
      ? <a href={entry.source.ref} target="_blank" rel="noreferrer">{github}</a> : entry.source.ref}</p>}
    {github && entry.source.merge_commit && <p className="knowledge-text">合并提交 · <code>{entry.source.merge_commit}</code></p>}
    <div className="knowledge-text">{entry.body}</div>
    {linkSkill && entry.source.type === "skill_registry" && <div className="knowledge-actions">
      <Link className={buttonVariants({ variant: "outline", size: "sm" })}
        to={skillPagePath(entry.project_id ?? undefined, entry.id)}>在 Skills 中打开</Link>
    </div>}
    {entry.relations.map((item) => <p key={`${item.direction}:${item.kind}:${item.target}`}>
      {item.direction === "in" ? "←" : "→"} {item.kind} · {item.title}
    </p>)}
  </div>;
}

function ReviewPanel({ scope, entry, onClose, refresh }: KnowledgeWriteProps & { entry: Entry }) {
  const decision = useMutation({
    mutationFn: (value: "accept" | "reject") => review(scope, entry, value),
    onSuccess: async () => { await refresh(); onClose(); },
  });
  return <Card title={entry.title} description="审核预览">
    <EntryContent entry={entry} />
    {decision.isError && <Banner closeIcon={null} type="danger" description={errorMessage(decision.error)} />}
    <div className="knowledge-actions">
      <Button disabled={decision.isPending} onClick={() => decision.mutate("reject")}>驳回</Button>
      <Button disabled={decision.isPending} theme="solid" onClick={() => decision.mutate("accept")}>通过并发布</Button>
    </div>
  </Card>;
}

/** 同一标签页沿用反馈会话 ID，重新打开详情时仍可更新该会话的反馈。 */
function getFeedbackSession() {
  const key = "weora.knowledge.feedback-session";
  try {
    const id = sessionStorage.getItem(key);
    if (id) return id;
    const created = crypto.randomUUID();
    sessionStorage.setItem(key, created);
    return created;
  } catch {
    return crypto.randomUUID();
  }
}

function EntryDialog({ scope, entry, user, onClose }: DialogProps & {
  entry: Entry; user: string;
}) {
  const loaded = useQuery({
    queryKey: ["knowledge-entry", scope.workspace, user, scope.project, entry.id, entry.revision],
    queryFn: () => detail(scope, entry.id),
  });
  const [session] = useState(getFeedbackSession);
  const vote = useMutation({
    mutationFn: (positive: boolean) => feedback(scope, entry.id, session, positive),
  });
  function submitVote(positive: boolean) {
    if (!vote.isPending) vote.mutate(positive);
  }

  return <Modal visible title={entry.title} onCancel={onClose}
    maskClosable={!vote.isPending} closeOnEsc={!vote.isPending}>
    {loaded.isPending && <Skeleton active />}
    {loaded.isError && <AssetLoadError title="正文暂时无法加载" description={errorMessage(loaded.error)}
      onRetry={() => void loaded.refetch()} />}
    {loaded.isSuccess && <>
      <EntryContent entry={loaded.data} linkSkill />
      <div className="knowledge-actions">
        {/* 反馈期间保留按键焦点，完成后仍可用 Escape 关闭详情。 */}
        <Button size="small" aria-disabled={vote.isPending} onClick={() => submitVote(true)}>有帮助</Button>
        <Button size="small" aria-disabled={vote.isPending} onClick={() => submitVote(false)}>未采用</Button>
        {vote.isSuccess && <span role="status" className="knowledge-meta">反馈已记录</span>}
      </div>
      {vote.isError && <Banner closeIcon={null} type="danger" description={errorMessage(vote.error)} />}
    </>}
  </Modal>;
}
