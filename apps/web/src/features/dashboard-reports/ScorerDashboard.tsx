import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Card, Empty, Select, Table, TabPane, Tabs } from "../../components/console";
import { readScorers, type ScoreFailure, type ScoreMeasure } from "./api";
import type { ReportPageProps } from "./DashboardReportsPage";
import {
  evidenceLabel, gitStartLabel, modeLabel, percent,
  ReportResult, ReportWindow, runtimeLabel, timestamp,
} from "./report-controls";
import { ScoreDistribution, ScoreMetrics, ScoreTrend } from "./scoring-sections";
import { WorkflowScorerDashboard } from "./WorkflowScorerDashboard";

type Measure = ScoreMeasure | ScoreFailure;
const measureKey = (value: Measure) => JSON.stringify([
  value.scorer_id, value.scorer_revision, value.source_hash, value.evidence, value.run_mode, value.driver, value.requested_model,
]);
const measureLabel = (value: Measure) => [
  "标准 v" + value.scorer_revision, evidenceLabel(value.evidence), modeLabel(value.run_mode),
  runtimeLabel(value.driver), value.requested_model && "请求模型 " + value.requested_model,
].filter(Boolean).join(" · ");

export function ScorerDashboard(props: ReportPageProps) {
  const [params, setParams] = useSearchParams();
  const active = params.get("source") === "workflow" ? "workflow" : "session";
  return <Tabs activeKey={active} className="report-stack scorer-source-tabs"
    onChange={(source) => setParams({
      ...(props.scope.project ? { project: props.scope.project } : {}),
      ...(source === "workflow" ? { source } : {}),
    })}>
    <TabPane itemKey="session" tab="会话评分"><SessionScorerDashboard {...props} /></TabPane>
    <TabPane itemKey="workflow" tab="工作流评分"><WorkflowScorerDashboard {...props} /></TabPane>
  </Tabs>;
}

function SessionScorerDashboard({ scope, user, scopeControl, canManageImprovements }: ReportPageProps) {
  const [days, setDays] = useState("30");
  const [selectedScorer, setScorer] = useState("");
  const [selectedMeasure, setMeasure] = useState("");
  const report = useQuery({
    queryKey: ["dashboard", "scorers", scope.workspace, user, scope.project, days], queryFn: () => readScorers(scope, days),
  });
  const cohorts = report.data?.cohorts ?? [];
  const options = Array.from(new Set(cohorts.map((row) => row.scorer))).sort();
  const scorer = options.includes(selectedScorer) ? selectedScorer : options[0] ?? "";
  const matches = cohorts.filter((row) => row.scorer === scorer);
  // 模型评审优先，默认选可见数据中的最新发布评分标准。
  const active = matches.find((row) => measureKey(row) === selectedMeasure)
    ?? matches.find((row) => row.driver !== "human") ?? matches[0];
  const failures = active ? (report.data?.failures ?? []).filter((row) => measureKey(row) === measureKey(active)) : [];
  const effectPath = "/self-improvement?" + new URLSearchParams({ project: scope.project, scorer }).toString();

  return <div className="report-stack">
    <div className="report-toolbar">
      {scopeControl}
      <ReportWindow days={days} options={[7, 30, 90, 180]} change={setDays} />
      <Select aria-label="选择评分器" value={scorer || undefined} disabled={!options.length}
        onChange={(value) => { setScorer(String(value)); setMeasure(""); }}
        optionList={options.map((value) => ({ value, label: value }))} />
    </div>
    <ReportResult pending={report.isPending} error={report.error} retry={() => void report.refetch()}>
      {active ? <>
        <ScoreMetrics stats={active.stats} />
        <Card title="评分口径">
          <Select aria-label="评分口径" value={measureKey(active)} onChange={(value) => setMeasure(String(value))}
            optionList={matches.map((row) => ({ value: measureKey(row), label: measureLabel(row) }))} />
          {active.source_hash && <p className="report-muted">评分标准摘要 <code title={active.source_hash}>{active.source_hash.slice(0, 12)}</code></p>}
          {active.agreement && <p className="report-muted">与作者标签一致 {active.agreement.agree}／{active.agreement.paired} 组配对</p>}
        </Card>
        <ScoreTrend weeks={active.weeks} />
        <ScoreDistribution labels={active.labels} />
        <Card title="按运行工具对比">
          <Table rowKey="runtime" variant="secondary" dataSource={active.by_runtime} columns={[
            { title: "工具", dataIndex: "runtime", render: runtimeLabel },
            { title: "通过率", dataIndex: "pass_rate", render: percent }, { title: "标签", dataIndex: "total" },
          ]} />
        </Card>
        <Card title="失败明细" headerExtraContent={scope.project && canManageImprovements && <Link to={effectPath}>查看自我改进</Link>}>
          {report.data?.truncated && <p className="report-muted">报表提供最近的失败明细，通过率使用完整时间窗口。</p>}
          <Table rowKey="label_id" variant="secondary" dataSource={failures} columns={[
            { title: "标签", dataIndex: "label" }, { title: "工具", dataIndex: "runtime", render: runtimeLabel },
            { title: "启动证据", dataIndex: "git_start", render: (value) => gitStartLabel(value.status) },
            { title: "评分时间", dataIndex: "scored_at", render: timestamp },
          ]} />
        </Card>
      </> : <Empty title="当前范围和时间窗口暂无评分标签" />}
    </ReportResult>
  </div>;
}
