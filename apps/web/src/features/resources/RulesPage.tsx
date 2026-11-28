import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, FileText, GitPullRequest, Plus, Search, Terminal } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Banner, Button, Card, Empty, Input, Modal, Select, Spin, Table, Tag } from "../../components/console";
import type { TableColumn } from "../../components/console/data-table";
import { useAuth } from "../auth/AuthProvider";
import { getChange, getSnapshot, listChanges, mergeChange, type ResourceSnapshot, type TeamRule } from "./api";
import { RuleChangeDialog, type RuleOperation } from "./RuleChangeDialog";

type RuleRow = TeamRule & { excluded: string | null };

function preview(snapshot: ResourceSnapshot, namespaces: ReadonlySet<string>, tags: ReadonlySet<string>): RuleRow[] {
  // 团队启用项目分区或成员分配时，共享根目录仍可用，未启用的命名空间留在目录中供核对。
  const scoped = namespaces.size > 0 || snapshot.projects.length > 0 || snapshot.members_configured;
  const visible = snapshot.rules.map((rule) => ({
    ...rule,
    excluded: scoped && rule.namespace && !namespaces.has(rule.namespace)
      ? "不在活动命名空间"
      : tags.size && rule.tags.length && !rule.tags.some((tag) => tags.has(tag)) ? "未订阅该规则标签" : null,
  }));
  const overrides = new Set(visible.filter((rule) => !rule.excluded && rule.namespace && rule.name.split("/").length === 2)
    .map((rule) => rule.name.split("/")[1]));
  return visible.map((rule) => ({
    ...rule,
    excluded: rule.excluded ?? (scoped && !rule.namespace && overrides.has(rule.name) ? "由活动命名空间的同名规则覆盖" : null),
  }));
}

export function RulesPage() {
  const { principal } = useAuth();
  const workspace = principal?.workspace.id ?? "";
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [tags, setTags] = useState<string[]>([]);
  const [rule, setRule] = useState<RuleRow | null>(null);
  const [localOpen, setLocalOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [review, setReview] = useState<{ workspace: string; changeId: string } | null>(null);
  const [draft, setDraft] = useState<{ workspace: string; snapshot: ResourceSnapshot; operation: RuleOperation } | null>(null);
  const query = useQuery({
    queryKey: ["team-resources", workspace], queryFn: ({ signal }) => getSnapshot(workspace, signal), enabled: !!workspace,
  });
  const snapshot = query.data;
  const project = snapshot?.projects.find(({ id }) => id === params.get("project"));
  const namespaces = new Set([...(project?.knowledge ?? []), ...(snapshot?.roles ?? [])
    .filter(({ id }) => snapshot?.member_roles.includes(id)).flatMap(({ knowledge }) => knowledge)]);
  const tagOptions = [...new Set(snapshot?.rules.flatMap((rule) => rule.tags) ?? [])];
  const rows = snapshot ? preview(snapshot, namespaces, new Set(tags)) : [];
  const eligible = rows.filter((row) => !row.excluded).length;
  const term = search.trim().toLocaleLowerCase();
  const displayed = rows.filter((row) => (status === "all" || (status === "yes") === !row.excluded) &&
    `${row.name} ${row.path} ${row.description}`.toLocaleLowerCase().includes(term));
  const commands = ["weora init --agent codex", ...(project ? [`weora projects set ${project.id}`] : []), "weora pull", "codex"]
    .join("\n");

  function startChange(operation: RuleOperation) {
    if (!snapshot || query.isError) return;
    // 固定打开时的默认修订，避免编辑期间刷新列表后无意覆盖并发修改。
    setDraft({ workspace, snapshot, operation });
    setRule(null);
  }
  const openRule = snapshot?.rules.find((entry) => entry.path === rule?.path);
  const columns: TableColumn<RuleRow>[] = [
    { title: "规则", dataIndex: "name", width: 320, render: (name, row) => (
      <button className="resource-rule-link" type="button" onClick={() => setRule(row)}>
        <FileText size={16} aria-hidden="true" />
        <span><strong>{name}</strong><code>{row.path}</code></span>
      </button>
    ) },
    { title: "命名空间", dataIndex: "namespace", width: 155, render: (scope) => <Tag>{scope ?? "共享根目录"}</Tag> },
    { title: "说明", dataIndex: "description", render: (description) => description || "—" },
    { title: "下发结果", dataIndex: "excluded", width: 235, render: (reason) => (
      <div className="resource-rule-result"><Tag color={reason ? "grey" : "green"}>{reason ? "不下发" : "应下发"}</Tag>
        {reason && <small>{reason}</small>}
      </div>
    ) },
  ];

  return (
    <div className="rules-page">
      <header className="page-header">
        <div><h1>Rules</h1><p>查看团队规则，以及选定项目会收到哪些规则。</p></div>
        <div className="resource-actions">
          <Button theme="light" icon={<GitPullRequest size={16} />} onClick={() => setReview({ workspace, changeId: "" })}>合并请求</Button>
          <Button theme="light" icon={<Terminal size={16} />} onClick={() => { setCopied(false); setLocalOpen(true); }}>
            在本地使用
          </Button>
          <Button theme="solid" type="primary" icon={<Plus size={16} />} disabled={!snapshot || query.isError}
            onClick={() => startChange({ kind: "create" })}>新增规则</Button>
        </div>
      </header>
      {query.isPending ? <Spin /> : query.isError ? (
        <Banner type="danger" title="规则加载失败" description={<>{query.error.message} <Button onClick={() => void query.refetch()}>重试</Button></>} />
      ) : (
        <>
          <Card>
            <div className="resource-scope">
              <div><span id="resource-project-label">预览项目</span>
                <Select aria-labelledby="resource-project-label" value={project?.id ?? ""}
                  optionList={[{ value: "", label: "当前成员的共享范围" }, ...(snapshot?.projects ?? []).map(({ id, name }) => ({ value: id, label: name }))]}
                  onChange={(value) => setParams(typeof value === "string" && value ? { project: value } : {})} />
              </div>
              <div><span>启用的命名空间</span><div className="resource-tags"><Tag>共享</Tag>
                {[...namespaces].map((namespace) => <Tag key={namespace} color="blue">{namespace}</Tag>)}
              </div><small>预览项目、角色和标签的下发范围。实际加载状态可在本地 CLI 检查。</small></div>
              {tagOptions.length > 0 && <div><span id="resource-tags-label">预览标签订阅</span>
                <Select aria-labelledby="resource-tags-label" multiple value={tags} placeholder="未订阅，全部匹配"
                  optionList={tagOptions.map((tag) => ({ value: tag, label: tag }))}
                  onChange={(value) => setTags(Array.isArray(value) ? value.map(String) : [])} />
              </div>}
            </div>
          </Card>
          <Card className="resource-catalog" title="团队规则" description={`${rows.length} 条团队规则 · ${eligible} 条应下发`} headerExtraContent={
            <div className="resource-filters">
              <Input aria-label="搜索规则" placeholder="搜索规则名称或路径" prefix={<Search size={16} />} value={search} onChange={setSearch} />
              <Select aria-label="下发状态" value={status} optionList={[{ value: "all", label: "全部状态" }, { value: "yes", label: "应下发" }, { value: "no", label: "不下发" }]}
                onChange={(value) => { if (typeof value === "string") setStatus(value); }} />
            </div>
          }>
            {displayed.length ? <Table columns={columns} dataSource={displayed} rowKey="path" /> : (
              <Empty title={rows.length ? "没有符合条件的规则" : "团队尚未发布规则"}
                description={rows.length ? "调整搜索词或下发状态。" : "新增规则后，工作空间管理员合并请求即可发布。"} />
            )}
          </Card>
        </>
      )}
      <Modal title="在本地使用" visible={localOpen} onCancel={() => setLocalOpen(false)}>
        <div className="resource-dialog"><p>在需要使用规则的本地代码仓库中运行。首次接入前，在 CLI 登录此业务空间。</p>
          <pre>{commands}</pre><p>首次启动 Codex 时，在 <code>/hooks</code> 审核并信任 Weora 的启动钩子。</p>
          <div className="resource-actions"><Button theme="light" icon={<Copy size={16} />} onClick={async () => {
            try { await navigator.clipboard.writeText(commands); setCopied(true); } catch { setCopied(false); }
          }}>{copied ? "已复制" : "复制命令"}</Button></div>
        </div>
      </Modal>
      <Modal title={rule?.name ?? "规则"} visible={!!rule} onCancel={() => setRule(null)}>
        {rule && <div className="resource-dialog"><code>{rule.path}</code>
          {rule.paths.length > 0 && <p>适用路径 <code>{rule.paths.join(", ")}</code></p>}
          {rule.tags.length > 0 && <div className="resource-tags">{rule.tags.map((tag) => <Tag key={tag}>{tag}</Tag>)}</div>}
          {rule.excluded && <p>{rule.excluded}</p>}<pre>{rule.body}</pre>
          {openRule && <div className="resource-actions">
            <Button theme="borderless" type="danger" onClick={() => startChange({ kind: "delete", rule: openRule })}>删除规则</Button>
            <Button theme="solid" type="primary" onClick={() => startChange({ kind: "edit", rule: openRule })}>修改规则</Button>
          </div>}
        </div>}
      </Modal>
      {draft?.workspace === workspace && <RuleChangeDialog {...draft} onClose={() => setDraft(null)} onSubmitted={(change) => {
        void client.invalidateQueries({ queryKey: ["resource-changes", draft.workspace] });
        setDraft(null);
        setReview({ workspace: draft.workspace, changeId: change.id });
      }} />}
      {review?.workspace === workspace && <ChangeReview workspace={workspace} initialChangeId={review.changeId}
        canMerge={principal?.workspace.role === "owner" || principal?.workspace.role === "admin"}
        onClose={() => setReview(null)} />}
    </div>
  );
}

function ChangeReview({ workspace, initialChangeId, canMerge, onClose }: {
  workspace: string; initialChangeId: string; canMerge: boolean; onClose: () => void;
}) {
  const client = useQueryClient();
  const [selected, setSelected] = useState(initialChangeId);
  const list = useQuery({ queryKey: ["resource-changes", workspace], queryFn: () => listChanges(workspace) });
  const detail = useQuery({ queryKey: ["resource-change", workspace, selected], queryFn: () => getChange(workspace, selected), enabled: !!selected });
  const merge = useMutation({ mutationFn: () => mergeChange(workspace, detail.data!), onSuccess: () => {
    void client.invalidateQueries({ queryKey: ["team-resources", workspace] });
    void client.invalidateQueries({ queryKey: ["resource-changes", workspace] });
    setSelected("");
  } });
  return <Modal title="团队源合并请求" visible onCancel={onClose} maskClosable={!merge.isPending} closeOnEsc={!merge.isPending}>
    <div className="resource-dialog">
      {initialChangeId && !merge.isSuccess && <Banner type="success" title="已提交合并请求，合并后生效" />}
      {list.isPending ? <Spin /> : list.isError ? <Banner type="danger" title={list.error.message} /> : list.data?.items.length ? (
        <Select aria-label="候选分支" placeholder="选择候选分支" value={selected}
          optionList={list.data.items.map(({ id, branch }) => ({ value: id, label: branch }))}
          onChange={(value) => { if (!merge.isPending && typeof value === "string") { merge.reset(); setSelected(value); } }} />
      ) : <Empty title="暂无待合并请求" />}
      {selected && detail.isPending && <Spin tip="正在读取差异…" />}
      {detail.isError && <Banner type="danger" title={detail.error.message} />}
      {detail.data?.changes.map((file) => <section key={file.path} className="resource-diff"><strong>{file.path}</strong>
        <div><span>变更前</span><pre>{file.before ?? "（无）"}</pre></div><div><span>变更后</span><pre>{file.after ?? "（已移除）"}</pre></div>
      </section>)}
      {merge.isError && <Banner type="danger" title={merge.error.message} />}
      {merge.isSuccess && <Banner type="success" title="已合并，新的修订已生效" />}
      <div className="resource-actions">
        <Button theme="light" disabled={merge.isPending} onClick={onClose}>关闭合并请求</Button>
        {canMerge && detail.data && <Button theme="solid" type="primary" loading={merge.isPending}
          onClick={() => merge.mutate()}>合并到默认修订</Button>}
      </div>
    </div>
  </Modal>;
}
