import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { Empty, Select, Typography } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import type { Scope } from "../knowledge/api";
import { listProjects, projectKey } from "../projects/api";
import type { PageKind } from "./api";
import { BenchmarkDashboard } from "./BenchmarkDashboard";
import { ImprovementDashboard } from "./ImprovementDashboard";
import { ReportResult } from "./report-controls";
import { ScorerDashboard } from "./ScorerDashboard";
import { SelfImprovementDashboard } from "./SelfImprovementDashboard";
import "./dashboard-reports.css";

const titles: Record<PageKind, string> = {
  improvement: "改进中心", "self-improvement": "自我改进", scorers: "评分器", benchmarks: "基准测试",
};
const panes = {
  improvement: ImprovementDashboard,
  "self-improvement": SelfImprovementDashboard,
  scorers: ScorerDashboard,
  benchmarks: BenchmarkDashboard,
};

export type ReportProps = { scope: Scope; user: string };
export type ReportPageProps = ReportProps & { scopeControl: ReactNode; canManageImprovements: boolean };

export function DashboardReportsPage({ kind }: { kind: PageKind }) {
  const { principal } = useAuth();
  if (!principal) throw new Error("团队报表缺少当前身份");
  const [params, setParams] = useSearchParams();
  const { workspace, user } = principal;
  const needsProject = kind === "self-improvement" || kind === "benchmarks";
  const canManageImprovements = workspace.role === "owner" || workspace.role === "admin";
  const canRead = kind === "scorers" || kind === "benchmarks" || canManageImprovements;
  const projects = useQuery({
    queryKey: projectKey(workspace.id, user.id), queryFn: () => listProjects(workspace.id), enabled: canRead,
  });
  const options = (projects.data?.items ?? []).filter((item) => item.status === "active")
    .map(({ id, name }) => ({ value: id, label: name }));
  const requested = params.get("project");
  const project = requested ?? (needsProject ? options[0]?.value : "") ?? "";
  const scope = { workspace: workspace.id, project };
  const Report = panes[kind];

  function changeProject(value: string) {
    // 深链中的实验或评分器属于原项目，切换范围后由新项目重新选择。
    setParams({
      ...(value === "shared" ? {} : { project: value }),
      ...(kind === "scorers" && params.get("source") === "workflow" ? { source: "workflow" } : {}),
    }, { replace: true });
  }

  return <section className="asset-page page-enter dashboard-reports">
    <header className="page-header"><Typography.Title heading={1}>{titles[kind]}</Typography.Title></header>
    {!canRead ? <Empty title="此报表仅向工作空间所有者和管理员开放" /> :
      <ReportResult pending={projects.isPending} error={projects.error} retry={() => void projects.refetch()}>
        {needsProject && !project ? <Empty title="当前工作空间没有可访问的项目" /> :
          <Report key={[kind, scope.workspace, user.id, project].join(":")} scope={scope} user={user.id}
            canManageImprovements={canManageImprovements} scopeControl={
              <Select aria-label="报表范围" value={project || "shared"} onChange={(value) => changeProject(String(value))}
                optionList={needsProject ? options : [{ value: "shared", label: "工作空间共享" }, ...options]} />
            } />}
      </ReportResult>}
  </section>;
}
