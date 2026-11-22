import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Button, Card, Empty, Input, Select, Skeleton, Tag } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { getProject, projectKey } from "../projects/api";
import { errorMessage } from "../runs/api";
import { codeModules, codeSources, type CodeModule, type Scope } from "./api";

export function KnowledgeSources({ scope, user, onReviews, onModule }: {
  scope: Scope; user: string; onReviews: () => void; onModule: (module: CodeModule) => void;
}) {
  const [sourceId, setSourceId] = useState("");
  const source = useQuery({
    queryKey: ["knowledge", scope.workspace, user, scope.project, "sources"],
    queryFn: () => codeSources(scope), enabled: !!scope.project,
  });
  const project = useQuery({
    queryKey: projectKey(scope.workspace, user, scope.project),
    queryFn: () => getProject(scope.workspace, scope.project), enabled: !!scope.project,
  });
  const repositories = new Map(project.data?.repositories.map((repo) => [repo.id, repo.name]));
  // 连接变化后，用最新可见列表选择来源，旧连接的模块不会继续显示。
  const options = (source.data?.items ?? []).map((item) => ({
    value: item.repository_id ?? item.codebase_id ?? "",
    label: item.name ?? repositories.get(item.repository_id ?? "") ?? "项目仓库", item,
  })).filter(({ value }) => !!value);
  const selected = options.find(({ value }) => value === sourceId) ?? options[0];
  const indexed = selected?.item;
  return <div className="knowledge-stack">
    <Card title="代码来源" headerExtraContent={indexed && <Tag color={indexed.freshness === "current" ? "green" : "grey"}>
      {!indexed.revision ? "未索引" : { current: "当前", stale: "待更新", unknown: "未知" }[indexed.freshness]}
    </Tag>}>
      {!scope.project ? <Empty title="选择项目后查看代码索引" />
        : source.isPending ? <Skeleton active />
        : source.isError ? <AssetLoadError title="代码来源暂时无法加载" description={errorMessage(source.error)}
            onRetry={() => void source.refetch()} />
        : !selected ? <Empty title="当前项目没有登记的代码来源" />
        : <CodeModuleList key={`${selected.value}:${indexed?.codebase_id ?? ""}`} scope={scope} user={user}
          codebase={indexed?.codebase_id} onModule={onModule}
          summary={[indexed?.source === "local_directory" ? "本机目录" : "登记仓库",
            indexed?.revision && `${indexed.indexed} 个文件 · ${indexed.source === "local_directory" ? "快照" : "提交"} ${indexed.revision.slice(0, 7)}`,
          ].filter(Boolean).join(" · ")}
          sourceSelect={<div className="knowledge-field"><span>仓库或本机目录</span>
            <Select aria-label="仓库或本机目录" optionList={options} value={selected.value}
              onChange={(value) => setSourceId(String(value))} />
          </div>} />}
    </Card>
    <Card title="GitHub 评审来源" description="合并后发布的经验保留 PR 地址与实际合并提交。"
      headerExtraContent={<Button size="small" onClick={onReviews}>在检索中查看评审经验</Button>} />
  </div>;
}

function CodeModuleList({ scope, user, codebase, sourceSelect, summary, onModule }: {
  scope: Scope; user: string; codebase: string | undefined; sourceSelect: ReactNode; summary: string;
  onModule: (module: CodeModule) => void;
}) {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const modules = useQuery({
    queryKey: ["knowledge", scope.workspace, user, scope.project, "modules", codebase, query],
    queryFn: () => codeModules(scope, codebase ?? "", query), enabled: !!codebase,
  });
  return <>
    <form className="knowledge-columns knowledge-source-filters" onSubmit={(event) => {
      event.preventDefault();
      setQuery(input.trim());
    }}>
      {sourceSelect}
      <div className="knowledge-field">
        <label>筛选模块<Input value={input} onChange={(value) => { setInput(value); if (!value.trim()) setQuery(""); }}
          placeholder="按模块名或路径搜索" maxLength={160} disabled={!codebase} /></label>
        <Button htmlType="submit" disabled={!codebase}>筛选</Button>
      </div>
    </form>
    <p className="knowledge-meta">{summary}</p>
    {!codebase ? <Empty title="此来源尚未生成代码索引" />
      : modules.isPending ? <Skeleton active />
      : modules.isError ? <AssetLoadError title="模块证据暂时无法加载" description={errorMessage(modules.error)}
          onRetry={() => void modules.refetch()} />
      : modules.data.items.length ? <>
        <ul className="knowledge-list">
          {modules.data.items.map((module) => <li key={module.entry_id} className="knowledge-row knowledge-directory-row">
            <div className="knowledge-row-copy"><strong className="knowledge-text">{module.title}</strong>
              <code className="knowledge-meta">{module.path}</code>
            </div>
            <Button size="small" theme="borderless" aria-label={`查看 ${module.title} 证据`}
              onClick={() => onModule(module)}>查看证据</Button>
          </li>)}
        </ul>
        {modules.data.truncated && <p className="knowledge-meta">显示前 100 个模块，可用名称或路径缩小范围。</p>}
      </> : <Empty title={query ? "没有匹配的模块" : "此索引暂无已发布模块证据"} />}
  </>;
}
