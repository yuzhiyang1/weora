import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Banner, Button, Card, Modal, Skeleton, Tag, TextArea } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { errorMessage } from "../runs/api";
import {
  namespaceKey, namespaceSettings, recallScope, saveNamespaceSettings, saveRecallScope,
  type NamespaceFolders, type NamespaceManifest, type Scope,
} from "./api";

const sections = [
  ["learnings", "经验"], ["knowledge", "规则"], ["docs", "文档"], ["wiki", "代码 Wiki"],
  ["skills", "Skill"], ["scorers", "评分器"],
] as const satisfies readonly (readonly [keyof NamespaceFolders, string])[];

export function KnowledgeDirectories({ scope, user, reviewer, scopeLabel }: {
  scope: Scope; user: string; reviewer: boolean; scopeLabel: string;
}) {
  const cache = useQueryClient();
  const [editor, setEditor] = useState<{ revision: number; manifest: NamespaceManifest }>();
  const settings = useQuery({ queryKey: namespaceKey(scope, user), queryFn: () => namespaceSettings(scope) });
  const recallKey = ["knowledge", scope.workspace, user, scope.project, "recall-scope"];
  const recall = useQuery({ queryKey: recallKey, queryFn: () => recallScope(scope), enabled: !!scope.project });
  const updateRecall = useMutation({
    mutationFn: saveRecallScope.bind(null, scope),
    onSuccess: (data) => cache.setQueryData(recallKey, data),
  });

  if (settings.isPending) return <Skeleton active />;
  if (settings.isError) return <AssetLoadError title="目录暂时无法加载" description={errorMessage(settings.error)}
    onRetry={() => void settings.refetch()} />;

  const { effective, revision, roles, members, projects } = settings.data;
  const canEdit = reviewer && revision !== undefined && roles && members && projects;
  return <div className="knowledge-columns">
    <Card title="我的生效目录" description={[effective?.role, scopeLabel].filter(Boolean).join(" · ")}>
      {effective ? <dl className="knowledge-list">
        {sections.map(([key, label]) => <div key={key} className="knowledge-row knowledge-directory-row">
          <dt>{label}</dt><dd className="knowledge-inline">
            {effective[key]?.length ? effective[key].map((folder) => <Tag key={folder}>{folder}</Tag>)
              : <span className="knowledge-meta">无专属目录</span>}
          </dd>
        </div>)}
      </dl> : <p className="knowledge-meta">工作空间尚未配置专属目录。</p>}
    </Card>
    {(reviewer || scope.project) && <Card title={reviewer ? "目录清单" : "项目召回范围"}
      description={reviewer ? "按成员职能和项目启用资源目录。" : undefined}
      headerExtraContent={canEdit && <Button onClick={() => setEditor({ revision, manifest: { roles, members, projects } })}>
        编辑清单
      </Button>}>
      {reviewer && <div className="knowledge-row"><span>清单版本</span><span className="knowledge-meta">{revision}</span></div>}
      {scope.project && (recall.isPending ? <Skeleton active /> : recall.isError
        ? <AssetLoadError title="召回设置无法加载" description={errorMessage(recall.error)}
          onRetry={() => void recall.refetch()} />
        : <div className="knowledge-row knowledge-directory-row">
          <span>项目自动召回共享知识</span>
          <div className="knowledge-inline">
            <Tag color={recall.data.inherit_shared ? "green" : "grey"}>{recall.data.inherit_shared ? "已开启" : "已关闭"}</Tag>
            {reviewer && <Button size="small" loading={updateRecall.isPending} disabled={updateRecall.isPending}
              onClick={() => updateRecall.mutate(!recall.data.inherit_shared)}>
              {recall.data.inherit_shared ? "关闭继承" : "启用继承"}
            </Button>}
          </div>
        </div>)}
      {updateRecall.isError && <Banner closeIcon={null} type="danger" description={errorMessage(updateRecall.error)} />}
    </Card>}
    {editor && <ManifestDialog {...editor} workspace={scope.workspace} onClose={() => setEditor(undefined)}
      refresh={() => cache.invalidateQueries({ queryKey: ["knowledge", scope.workspace, user] })} />}
  </div>;
}

function parseManifest(text: string): NamespaceManifest {
  const fields = ["roles", "members", "projects"] as const;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("目录清单必须是有效的 JSON。");
  }
  if (!parsed || typeof parsed !== "object" || Object.keys(parsed).length !== fields.length ||
      !fields.every((field) => {
        const value: unknown = Reflect.get(parsed, field);
        return value !== null && typeof value === "object" && !Array.isArray(value);
      })) {
    throw new Error("清单必须包含 roles、members 和 projects 三个对象。");
  }
  return parsed as NamespaceManifest;
}

function ManifestDialog({ workspace, revision, manifest, onClose, refresh }: {
  workspace: string; revision: number; manifest: NamespaceManifest; onClose: () => void; refresh: () => Promise<void>;
}) {
  const [draft, setDraft] = useState(() => JSON.stringify(manifest, null, 2));
  const save = useMutation({
    // 编辑和重试沿用打开时的版本，避免覆盖另一位管理员刚保存的清单。
    mutationFn: () => saveNamespaceSettings(workspace, revision, parseManifest(draft)),
    onSuccess: async () => { await refresh(); onClose(); },
    onError: () => void refresh(),
  });

  return <Modal visible title="编辑目录清单" onCancel={onClose} maskClosable={!save.isPending} closeOnEsc={!save.isPending}>
    <form className="knowledge-form" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <p className="knowledge-meta">版本 {revision} · 这份清单适用于整个工作空间。</p>
      <label>目录清单 JSON<TextArea rows={12} value={draft} onChange={setDraft} disabled={save.isPending} /></label>
      {save.isError && <Banner closeIcon={null} type="danger" description={errorMessage(save.error)} />}
      <div className="knowledge-actions knowledge-dialog-footer">
        <Button onClick={onClose} disabled={save.isPending}>取消</Button>
        <Button htmlType="submit" theme="solid" loading={save.isPending} disabled={save.isPending || !draft.trim()}>
          保存清单
        </Button>
      </div>
    </form>
  </Modal>;
}
