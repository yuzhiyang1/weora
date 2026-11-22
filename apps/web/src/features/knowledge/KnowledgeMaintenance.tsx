import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Banner, Button, Input, Modal, Select, Skeleton, TextArea } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { errorMessage } from "../runs/api";
import {
  archive, knowledgeTypes, maintenance, promotionPreview, qualityPreview, submitPromotion, submitQuality,
  type Candidate, type MaintenanceType, type PromotionContext, type PromotionKind, type Scope,
} from "./api";
import { KnowledgeRows } from "./KnowledgeRows";

const panels = {
  promote: { title: "晋升候选", action: "提交晋升评审", empty: "当前没有达到晋升条件的经验" },
  prune: { title: "低置信度经验", action: "归档预演", empty: "当前没有建议归档的经验" },
  quality: { title: "待修订条目", action: "提交修订", empty: "当前没有待修订条目" },
} as const;
type Selection = { kind: MaintenanceType; item: Candidate };
type ActionProps = { scope: Scope; user: string; selection: Selection; close: () => void; done: () => Promise<void> };
const maintenanceKey = (scope: Scope, user: string) => ["knowledge", scope.workspace, user, scope.project, "maintenance"];

export function KnowledgeMaintenance({ scope, user, reviewer, refresh }: {
  scope: Scope; user: string; reviewer: boolean; refresh: () => Promise<void>;
}) {
  const [selection, setSelection] = useState<Selection>();
  const [notice, setNotice] = useState("");
  const kinds: MaintenanceType[] = reviewer ? ["promote", "prune", "quality"] : ["promote"];

  async function finishMaintenance(kind: MaintenanceType) {
    // 维护会改变候选列表，晋升和修订还会新增待审核版本。
    await refresh();
    setNotice(kind === "prune" ? "经验已归档" : "已提交待审核版本");
    setSelection(undefined);
  }

  return <div className="knowledge-stack">
    {notice && <Banner closeIcon={null} type="success" description={notice} />}
    {kinds.map((kind) => <CandidateList key={kind} scope={scope} user={user} kind={kind} onSelect={(item) => {
      setNotice("");
      setSelection({ kind, item });
    }} />)}
    {selection && <MaintenanceAction key={`${selection.kind}:${selection.item.id}`}
      scope={scope} user={user} selection={selection} close={() => setSelection(undefined)}
      done={() => finishMaintenance(selection.kind)} />}
  </div>;
}

function CandidateList({ scope, user, kind, onSelect }: {
  scope: Scope; user: string; kind: MaintenanceType; onSelect: (entry: Candidate) => void;
}) {
  const query = useInfiniteQuery({
    queryKey: [...maintenanceKey(scope, user), kind],
    queryFn: ({ pageParam }) => maintenance(scope, kind, pageParam),
    initialPageParam: "",
    getNextPageParam: (page) => page.next_after || undefined,
  });
  if (query.isPending) return <Skeleton active />;
  if (query.isError) return <AssetLoadError title={`${panels[kind].title}暂时无法加载`}
    description={errorMessage(query.error)} onRetry={() => void query.refetch()} />;
  return <KnowledgeRows title={panels[kind].title} empty={panels[kind].empty}
    rows={query.data.pages.flatMap((page) => page.items).map((item) => ({
      id: item.id, title: item.title,
      meta: [
        typeof item.confidence === "number" ? `置信度 ${item.confidence.toFixed(2)}` : null,
        typeof item.recalls === "number" ? `召回 ${item.recalls} 次` : null,
        typeof item.upvotes === "number" ? `采纳 ${item.upvotes} 次` : null,
      ].filter(Boolean).join(" · "),
      action: <Button size="small" onClick={() => onSelect(item)}>{panels[kind].action}</Button>,
    }))}
    more={query.hasNextPage && <Button size="small" loading={query.isFetchingNextPage}
      onClick={() => void query.fetchNextPage()}>加载更多</Button>} />;
}

function MaintenanceAction(props: ActionProps) {
  if (props.selection.kind === "prune") return <ArchivePreview {...props} />;
  return props.selection.kind === "promote" ? <PromotionPreview {...props} /> : <QualityPreview {...props} />;
}

function ArchivePreview({ scope, selection, close, done }: ActionProps) {
  const { id, title } = selection.item;
  const action = useMutation({
    mutationFn: async () => {
      // 服务端会复核归档资格，只有回执确认当前条目已归档才刷新成功状态。
      const result = await archive(scope, id);
      const archived = result.items.some((item) => item.id === id && item.status === "archived");
      if (!archived) {
        throw new Error("该经验的归档资格已变化，请刷新维护列表后重试。");
      }
    },
    onSuccess: done,
  });
  return <Modal title="归档预演" visible onCancel={close} maskClosable={!action.isPending} closeOnEsc={!action.isPending}>
    <div className="knowledge-form">
      <strong className="knowledge-text">{title}</strong>
      <p className="knowledge-meta">归档后将停止召回这条经验，已发布版本和审计记录仍可追查。</p>
      {action.isError && <Banner closeIcon={null} type="danger" description={errorMessage(action.error)} />}
      <div className="knowledge-actions">
        <Button onClick={close} disabled={action.isPending}>取消</Button>
        <Button type="danger" onClick={() => action.mutate()} loading={action.isPending}>确认归档</Button>
      </div>
    </div>
  </Modal>;
}

function PromotionPreview(props: ActionProps) {
  const { scope, user, selection, close, done } = props;
  const preview = useQuery({
    queryKey: [...maintenanceKey(scope, user), "preview", selection.item.id, "promote"],
    queryFn: () => promotionPreview(scope, selection.item.id),
  });
  if (preview.isSuccess) return <PromotionForm scope={scope} context={preview.data} close={close} done={done} />;
  return <Modal title="晋升预览" visible onCancel={close}>{preview.isError ?
    <AssetLoadError title="晋升预览暂时无法加载" description={errorMessage(preview.error)}
      onRetry={() => void preview.refetch()} /> : <Skeleton active />}</Modal>;
}

function PromotionForm({ scope, context, close, done }: {
  scope: Scope; context: PromotionContext; close: () => void; done: () => Promise<void>;
}) {
  const options = (["rule", "doc", "skill"] as const).map((value) => ({
    value, label: knowledgeTypes[value], disabled: context.occupied_kinds.includes(value),
  }));
  // 已有晋升提案的目标不可再次选择，推荐类型被占用时尝试下一个可用目标。
  const recommendedKind = context.kind;
  const firstAvailableKind = options.find((option) => !option.disabled)?.value;
  const [kind, setKind] = useState<PromotionKind>(context.occupied_kinds.includes(recommendedKind)
    ? firstAvailableKind ?? recommendedKind
    : recommendedKind);
  const [reason, setReason] = useState("");
  const [body, setBody] = useState(context.body);
  const [operation] = useState(() => crypto.randomUUID());
  const submit = useMutation({
    mutationFn: () => submitPromotion(scope, context.id, {
      operation_id: operation, source_revision: context.revision, kind, reason: reason.trim(), body,
    }),
    onSuccess: done,
  });
  return <Modal title="晋升预览" visible onCancel={close} maskClosable={!submit.isPending} closeOnEsc={!submit.isPending}>
    <form className="knowledge-form" onSubmit={(event) => { event.preventDefault(); submit.mutate(); }}>
      <p className="knowledge-text">{context.title} · 来源版本 {context.revision}</p>
      <Select aria-label="晋升类型" optionList={options} value={kind} disabled={submit.isPending}
        onChange={(value) => setKind(value as PromotionKind)} />
      <p className="knowledge-meta knowledge-text">目标 · {context.targets[kind]}</p>
      <label>晋升理由<Input value={reason} required maxLength={500} onChange={setReason} disabled={submit.isPending} /></label>
      <label>正文<TextArea value={body} required maxLength={64000} rows={7}
        onChange={setBody} disabled={submit.isPending} /></label>
      {submit.isError && <Banner closeIcon={null} type="danger" description={errorMessage(submit.error)} />}
      <div className="knowledge-actions">
        <Button onClick={close} disabled={submit.isPending}>取消</Button>
        <Button htmlType="submit" theme="solid" loading={submit.isPending}
          disabled={!reason.trim() || !body.trim() || context.occupied_kinds.includes(kind)}>提交晋升评审</Button>
      </div>
    </form>
  </Modal>;
}

function QualityPreview({ scope, user, selection, close, done }: ActionProps) {
  const preview = useQuery({
    queryKey: [...maintenanceKey(scope, user), "preview", selection.item.id, "quality"],
    queryFn: () => qualityPreview(scope, selection.item.id),
  });
  if (preview.isSuccess) return <QualityForm scope={scope} context={preview.data} close={close} done={done} />;
  return <Modal title="修订知识" visible onCancel={close}>{preview.isError ?
    <AssetLoadError title="原始版本暂时无法加载" description={errorMessage(preview.error)}
      onRetry={() => void preview.refetch()} /> : <Skeleton active />}</Modal>;
}

function QualityForm({ scope, context, close, done }: {
  scope: Scope; context: Awaited<ReturnType<typeof qualityPreview>>; close: () => void; done: () => Promise<void>;
}) {
  const [body, setBody] = useState(context.body);
  const [operation] = useState(() => crypto.randomUUID());
  const submit = useMutation({
    mutationFn: () => {
      // Registry Skill 与知识条目使用各自的版本标识，服务端用它防止提交基于旧版本的修订。
      const submission = { operation_id: operation, body };
      if (context.skill_version_id) {
        return submitQuality(scope, context.id, { ...submission, base_skill_version_id: context.skill_version_id });
      }
      if (context.revision !== undefined) {
        return submitQuality(scope, context.id, { ...submission, base_revision: context.revision });
      }
      return submitQuality(scope, context.id, submission);
    },
    onSuccess: done,
  });
  return <Modal title="修订知识" visible onCancel={close} maskClosable={!submit.isPending} closeOnEsc={!submit.isPending}>
    <form className="knowledge-form" onSubmit={(event) => { event.preventDefault(); submit.mutate(); }}>
      <p className="knowledge-text">{context.title}</p>
      <label>修订正文<TextArea value={body} required maxLength={64000} rows={8}
        onChange={setBody} disabled={submit.isPending} /></label>
      {submit.isError && <Banner closeIcon={null} type="danger" description={errorMessage(submit.error)} />}
      <div className="knowledge-actions">
        <Button onClick={close} disabled={submit.isPending}>取消</Button>
        <Button htmlType="submit" theme="solid" loading={submit.isPending}
          disabled={!body.trim() || body === context.body}>提交修订</Button>
      </div>
    </form>
  </Modal>;
}
