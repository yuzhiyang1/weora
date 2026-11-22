import { useQuery } from "@tanstack/react-query";
import { Button, Card, Empty, Skeleton, Tag } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { getProject, projectKey } from "../projects/api";
import { errorMessage } from "../runs/api";
import { codeSources, type Scope } from "./api";
import { KnowledgeRows } from "./KnowledgeRows";

export function KnowledgeSources({ scope, user, onReviews }: { scope: Scope; user: string; onReviews: () => void }) {
  const source = useQuery({
    queryKey: ["knowledge", scope.workspace, user, scope.project, "sources"],
    queryFn: () => codeSources(scope), enabled: !!scope.project,
  });
  const project = useQuery({
    queryKey: projectKey(scope.workspace, user, scope.project),
    queryFn: () => getProject(scope.workspace, scope.project), enabled: !!scope.project,
  });
  const repositories = new Map(project.data?.repositories.map((repo) => [repo.id, repo.name]));
  return <div className="knowledge-stack">
    {!scope.project ? <Card title="代码来源"><Empty title="选择项目后查看代码索引" /></Card>
      : source.isPending ? <Skeleton active />
      : source.isError ? <AssetLoadError title="代码来源暂时无法加载" description={errorMessage(source.error)}
          onRetry={() => void source.refetch()} />
      : <KnowledgeRows title="代码来源" empty="当前项目没有登记的代码来源"
        rows={source.data.items.map((item) => ({
          id: item.repository_id ?? item.codebase_id ?? item.revision ?? "",
          title: item.name ?? repositories.get(item.repository_id ?? "") ?? "项目仓库",
          meta: <div className="knowledge-inline">
            <span>{item.source === "local_directory" ? "本机目录" : "登记仓库"}</span>
            <Tag color={item.freshness === "current" && item.revision ? "green" : "grey"}>
              {!item.revision ? "未索引" : { current: "当前", stale: "待更新", unknown: "未知" }[item.freshness]}
            </Tag>
            {item.revision && <span>{item.indexed} 个文件 · 提交 {item.revision.slice(0, 7)}</span>}
          </div>,
        }))} />}
    <Card title="GitHub 评审来源">
      <div className="knowledge-stack">
        <p className="knowledge-meta">合并后发布的经验保留 PR 地址与实际合并提交。</p>
        <div><Button size="small" onClick={onReviews}>在检索中查看评审经验</Button></div>
      </div>
    </Card>
  </div>;
}
