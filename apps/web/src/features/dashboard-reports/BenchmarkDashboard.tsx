import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Button, Card, Empty, Modal, Select, Table, TabPane, Tabs } from "../../components/console";
import { readBenchmarkRun, readBenchmarks, type Arm, type ArmRate, type BenchmarkReport } from "./api";
import type { ReportPageProps, ReportProps } from "./DashboardReportsPage";
import {
  armLabel, gateLabel, Metrics, number, percent, ReportChart, ReportPagination, ReportResult,
  ReportWindow, runLabel, runtimeLabel, timestamp,
} from "./report-controls";

function totals(rows: ArmRate[], arm: Arm) {
  const group = rows.filter((item) => item.arm === arm);
  const planned = group.reduce((sum, item) => sum + item.planned, 0);
  const passed = group.reduce((sum, item) => sum + item.passed, 0);
  return { planned, passed, rate: planned ? passed / planned : null };
}

const interval = (bounds: ArmRate["interval95"]) => bounds ? bounds.map(percent).join("–") : "—";
const artifact = (value: string) => <code title={value}>{value.slice(0, 12)}</code>;

export function BenchmarkDashboard({ scope, user, scopeControl }: ReportPageProps) {
  const [params, setParams] = useSearchParams();
  const suite = params.get("suite") ?? "";
  const run = params.get("run") ?? "";
  const [days, setDays] = useState("90");
  const [offset, setOffset] = useState(0);
  const key = ["dashboard", "benchmarks", scope.workspace, user, scope.project];
  const report = useQuery({
    queryKey: [...key, suite, days, offset], queryFn: () => readBenchmarks(scope, days, suite, offset),
  });
  const data = report.data;

  function navigate(value: string, runId?: string) {
    setParams({
      project: scope.project, ...(value ? { suite: value } : {}), ...(runId ? { run: runId } : {}),
    }, { replace: true });
  }

  return <div className="report-stack">
    <div className="report-toolbar">
      {scopeControl}
      <ReportWindow days={days} options={[30, 90, 365]} change={(value) => { setDays(value); setOffset(0); }} />
      <Select aria-label="基准套件" value={data?.suite_id ?? (suite || undefined)} disabled={!data?.suites.length}
        onChange={(value) => { setOffset(0); navigate(String(value)); }}
        optionList={(data?.suites ?? []).map(({ id, path }) => ({ value: id, label: path }))} />
    </div>
    <ReportResult pending={report.isPending} error={report.error} retry={() => void report.refetch()}>
      {data?.suite_id ? <BenchmarkContents data={data} offset={offset} setOffset={setOffset}
        openRun={(id) => navigate(data.suite_id ?? "", id)} /> : <Empty title="当前项目暂无已登记的回归套件" />}
    </ReportResult>
    {run && <RunDetail id={run} scope={scope} user={user} close={() => navigate(suite)} />}
  </div>;
}

function BenchmarkContents({ data, offset, setOffset, openRun }: {
  data: BenchmarkReport; offset: number; setOffset: (value: number) => void; openRun: (id: string) => void;
}) {
  const main = totals(data.arms, "main");
  const pulled = totals(data.arms, "pulled");
  const compared = data.comparisons.reduce((sum, row) => sum + row.compared, 0);
  const description = data.suites.find((suite) => suite.id === data.suite_id)?.description;
  const arms = data.arms.map((row) => ({ ...row, id: [row.tool, row.arm].join(":"), label: runtimeLabel(row.tool) + " · " + armLabel[row.arm] }));
  const comparisons = data.comparisons.map((row) => ({ ...row, id: JSON.stringify([row.task, row.starting_commit, row.tool]) }));
  const byTask = data.tasks.map((row) => ({ ...row, id: JSON.stringify([row.task, row.tool, row.arm]) }));
  const byRevision = data.revisions.map((row) => ({ ...row, id: [row.revision, row.tool, row.arm].join(":") }));
  const [tab, setTab] = useState("arms");
  return <>
    {description && <p className="report-muted">{description}</p>}
    <Metrics items={[
      { label: "结束运行", value: number(data.total), detail: "当前套件和时间窗口" },
      { label: "基线通过率", value: percent(main.rate), detail: number(main.passed) + "／" + number(main.planned) + " 次计划试验" },
      { label: "改动组通过率", value: percent(pulled.rate), detail: number(pulled.passed) + "／" + number(pulled.planned) + " 次计划试验" },
      { label: "可比较配对", value: number(compared), detail: "评审工具、模型和 effort 相同" },
    ]} />
    <Card title="基准对比">
      <Tabs activeKey={tab} onChange={setTab}>
        <TabPane itemKey="arms" tab="实验组">
          <Table rowKey="id" variant="secondary" dataSource={arms} columns={[
            { title: "实验组", dataIndex: "label" }, { title: "计划", dataIndex: "planned" },
            { title: "已评审", dataIndex: "judged" }, { title: "通过率", dataIndex: "pass_rate", render: percent },
            { title: "95% 置信区间", dataIndex: "interval95", render: interval },
          ]} />
        </TabPane>
        <TabPane itemKey="compare" tab="对比">
          <Table rowKey="id" variant="secondary" dataSource={comparisons} columns={[
            { title: "任务", dataIndex: "task" }, { title: "起点", dataIndex: "starting_commit", render: artifact },
            { title: "工具", dataIndex: "tool", render: runtimeLabel }, { title: "可比较配对", dataIndex: "compared" },
            { title: "基线", dataIndex: "main", render: (value) => percent(value.pass_rate) },
            { title: "改动后", dataIndex: "pulled", render: (value) => percent(value.pass_rate) },
            { title: "评审器不同", dataIndex: "excluded_judge_changed" }, { title: "未评审", dataIndex: "unjudged" },
          ]} />
        </TabPane>
        <TabPane itemKey="tasks" tab="任务">
          <Table rowKey="id" variant="secondary" dataSource={byTask} columns={[
            { title: "任务", dataIndex: "task" }, { title: "工具", dataIndex: "tool", render: runtimeLabel },
            { title: "组别", dataIndex: "arm", render: (value) => armLabel[value] },
            { title: "计划", dataIndex: "planned" }, { title: "通过率", dataIndex: "pass_rate", render: percent },
          ]} />
        </TabPane>
        <TabPane itemKey="revisions" tab="团队变更">
          <Table rowKey="id" variant="secondary" dataSource={byRevision} columns={[
            { title: "资源提交", dataIndex: "revision", render: artifact },
            { title: "工具", dataIndex: "tool", render: runtimeLabel },
            { title: "组别", dataIndex: "arm", render: (value) => armLabel[value] },
            { title: "计划", dataIndex: "planned" }, { title: "通过率", dataIndex: "pass_rate", render: percent },
          ]} />
        </TabPane>
      </Tabs>
    </Card>
    <Card title="运行通过率">
      <p className="report-muted">本页历史 · 基线／改动后</p>
      <ReportChart label="基准运行通过率" format={percent} ceiling={1} points={data.runs.slice().reverse().map((row) => ({
        id: row.id, label: new Date(row.ended_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" }),
        values: [totals(row.arms, "main").rate, totals(row.arms, "pulled").rate],
      }))} />
    </Card>
    <Card title="运行历史">
      {data.runs.length ? <Table rowKey="id" variant="secondary" dataSource={data.runs} columns={[
        { title: "结束时间", dataIndex: "ended_at", render: timestamp },
        { title: "基线", key: "main", render: (_, row) => percent(totals(row.arms, "main").rate) },
        { title: "改动后", key: "pulled", render: (_, row) => percent(totals(row.arms, "pulled").rate) },
        { title: "状态", dataIndex: "status", render: (value) => runLabel[value] },
        { title: "操作", key: "open", render: (_, row) => <Button theme="borderless" size="small"
          onClick={() => openRun(row.id)}>查看运行</Button> },
      ]} /> : <Empty title="此套件在当前时间窗口没有已结束的运行" />}
      <ReportPagination offset={offset} next={data.next_offset} total={data.total} count={data.runs.length} change={setOffset} />
    </Card>
  </>;
}

function RunDetail({ scope, user, id, close }: ReportProps & { id: string; close: () => void }) {
  const query = useQuery({
    queryKey: ["dashboard", "benchmark-run", scope.workspace, user, scope.project, id],
    queryFn: () => readBenchmarkRun(scope, id),
  });
  const trialLabel = (outcome: string) => ({
    passed: "通过", failed: "失败", error: "错误", unscored: "未评分", not_run: "未运行",
  }[outcome] ?? outcome);
  const data = query.data;
  return <Modal title="基准运行详情" visible onCancel={close} width={930}>
    <ReportResult pending={query.isPending} error={query.error} retry={() => void query.refetch()}>
      {data && <div className="report-detail">
        <p>{data.suite} · {runLabel[data.status]} · {gateLabel[data.gate]}</p>
        <p>基线 {artifact(data.base_resources)} · 改动后 {artifact(data.proposal_resources)}</p>
        <Table rowKey="id" variant="secondary" dataSource={data.trials} columns={[
          { title: "任务", dataIndex: "task" }, { title: "工具", dataIndex: "tool", render: runtimeLabel },
          { title: "组别", dataIndex: "arm", render: (value) => armLabel[value] },
          { title: "次数", dataIndex: "repetition" }, { title: "结果", dataIndex: "outcome", render: trialLabel },
          { title: "评审", dataIndex: "judge_tool", render: (value, row) =>
            [value && runtimeLabel(value), row.judge_model, row.judge_effort].filter(Boolean).join(" · ") || "—" },
        ]} />
      </div>}
    </ReportResult>
  </Modal>;
}
