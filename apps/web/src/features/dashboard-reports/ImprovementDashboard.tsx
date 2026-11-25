import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Button, Card, Empty, Select, Table, Tag } from "../../components/console";
import { readImprovement, type Day, type ImprovementReport } from "./api";
import type { ReportPageProps } from "./DashboardReportsPage";
import { cost, dayLabel, Metrics, number, percent, ReportChart, ReportResult, ReportWindow, timestamp } from "./report-controls";

const activityFields = {
  sessions_ended: { label: "结束会话", display: number },
  known_cost_micros: { label: "已知费用", display: cost },
  prompts: { label: "对话轮数", display: number },
  active_members: { label: "活跃成员", display: number },
} satisfies Partial<Record<keyof Day, { label: string; display: typeof number }>>;
const graphs = Object.entries(activityFields).map(([value, { label }]) => ({ value, label }));
const adoptionGroups = { adopted: "已使用", unused: "未使用", archived_in_use: "已归档仍有使用记录" } as const;
const maintenanceLabels = { promote: "可晋升", prune: "建议归档", quality: "待改进" } as const;

function movement(current: number | null, previous: number | null, asPoints = false) {
  if (current === null || previous === null) return "上期暂无可比较数据";
  const delta = current - previous;
  if (delta === 0) return "与上期持平";
  return "较上期 " + (delta > 0 ? "+" : "−") + number(Math.abs(delta) * (asPoints ? 100 : 1))
    + (asPoints ? " 个百分点" : "");
}

export function ImprovementDashboard({ scope, user, scopeControl }: ReportPageProps) {
  const [days, setDays] = useState("30");
  const [field, setField] = useState<keyof typeof activityFields>("sessions_ended");
  const [adoption, setAdoption] = useState<keyof typeof adoptionGroups>("adopted");
  const report = useQuery({
    queryKey: ["dashboard", "improvement", scope.workspace, user, scope.project, days],
    queryFn: () => readImprovement(scope, days),
  });
  const display = activityFields[field];
  const maintenanceLink = "/knowledge?" + new URLSearchParams({
    view: "maintenance", ...(scope.project ? { project: scope.project } : {}),
  }).toString();
  const openSkills = "/skills" + (scope.project ? "?" + new URLSearchParams({ project: scope.project }).toString() : "");

  return <div className="report-stack">
    <div className="report-toolbar">
      {scopeControl}
      <ReportWindow days={days} options={[7, 30, 90]} change={setDays} />
      <Button theme="solid" disabled={report.isFetching} onClick={() => void report.refetch()}>刷新报告</Button>
      {report.data && <span className="report-muted">更新于 {timestamp(report.data.generated_at)}</span>}
    </div>
    <ReportResult pending={report.isPending} error={report.error} retry={() => void report.refetch()}>
      {report.data && <ImprovementContents data={report.data} field={field} display={display}
        setField={setField} adoption={adoption} setAdoption={setAdoption} maintenanceLink={maintenanceLink} openSkills={openSkills} />}
    </ReportResult>
  </div>;
}

function ImprovementContents({ data, field, display, setField, adoption, setAdoption, maintenanceLink, openSkills }: {
  data: ImprovementReport; field: keyof typeof activityFields; display: (typeof activityFields)[keyof typeof activityFields];
  setField: (field: keyof typeof activityFields) => void;
  adoption: keyof typeof adoptionGroups; setAdoption: (value: keyof typeof adoptionGroups) => void;
  maintenanceLink: string; openSkills: string;
}) {
  const { current, previous, daily, maintenance } = data;
  const chosen = data.skill_adoption[adoption];
  const actions = (Object.keys(maintenanceLabels) as (keyof typeof maintenanceLabels)[]).flatMap((key) =>
    maintenance[key].items.map((entry) => ({ ...entry, key: key + ":" + entry.id, action: maintenanceLabels[key] })));
  return <>
    <Metrics items={[
      { label: "结束会话", value: number(current.sessions_ended), detail: movement(current.sessions_ended, previous.sessions_ended) },
      { label: "成功率", value: percent(current.success_rate), detail: movement(current.success_rate, previous.success_rate, true) },
      { label: "纠偏率", value: percent(current.correction_rate), detail: movement(current.correction_rate, previous.correction_rate, true) },
      { label: "已知费用", value: cost(current.known_cost_micros), detail: "日均 " + cost(current.daily_known_cost_micros)
        + " · " + number(current.priced_requests) + " 次计价请求" },
    ]} />
    <Card title="团队活动">
      <div className="report-stack">
        <div className="report-inline">
          <Select aria-label="团队活动指标" optionList={graphs} value={field}
            onChange={(value) => setField(String(value) as keyof typeof activityFields)} />
          <span className="report-muted">平均 {number(current.avg_prompts)} 轮／会话 · 缓存读取占比 {percent(current.cache_read_share)}</span>
        </div>
        <ReportChart label={display.label + "每日趋势"} format={display.display}
          points={daily.map((row) => ({ id: row.day, label: dayLabel(row.day), values: [row[field]] }))} />
      </div>
      <details className="report-details">
        <summary>查看每日明细</summary>
        <Table rowKey="day" variant="secondary" dataSource={daily} columns={[
          { title: "UTC 日期", dataIndex: "day" }, { title: "结束会话", dataIndex: "sessions_ended" },
          { title: "有纠偏", dataIndex: "sessions_with_correction" }, { title: "对话轮数", dataIndex: "prompts" },
          { title: "活跃成员", dataIndex: "active_members" }, { title: "已知费用", dataIndex: "known_cost_micros", render: cost },
        ]} />
      </details>
    </Card>
    <Card title="团队 Skill 采纳 · 近 30 天">
      <div className="report-stack">
        <div className="report-inline">
          <Select aria-label="Skill 使用状态" value={adoption}
            onChange={(value) => setAdoption(String(value) as keyof typeof adoptionGroups)}
            optionList={(Object.keys(adoptionGroups) as (keyof typeof adoptionGroups)[]).map((key) =>
              ({ value: key, label: adoptionGroups[key] + " · " + data.skill_adoption[key].total }))} />
          <Link to={openSkills}>查看 Skills</Link>
        </div>
        {chosen.total ? <Table rowKey="id" variant="secondary" dataSource={chosen.items} columns={[
          { title: "Skill", dataIndex: "name" }, { title: "使用成员", dataIndex: "members" },
          { title: "使用轮次", dataIndex: "uses" }, { title: "最近使用", dataIndex: "last_used_at", render: timestamp },
        ]} /> : <Empty title="当前分类暂无 Skill" />}
        {chosen.total > chosen.items.length && <p className="report-muted">显示前 {chosen.items.length} 个，共 {chosen.total} 个</p>}
      </div>
    </Card>
    <Card title="摩擦信号">
      <p>{number(current.corrections)} 次纠正 · {number(current.interrupts)} 次中断 · {number(current.tool_rejections)} 次拒绝工具调用</p>
      <p className="report-muted">{number(current.sessions_with_intervention)} 次会话发生干预，占 {percent(
        current.sessions_ended ? current.sessions_with_intervention / current.sessions_ended : null,
      )}</p>
    </Card>
    <Card title="知识维护" headerExtraContent={<Link to={maintenanceLink}>处理维护候选</Link>}>
      <div className="report-inline">
        <Tag color="blue">可晋升 {maintenance.promote.total}</Tag>
        <Tag color="grey">建议归档 {maintenance.prune.total}</Tag>
        <Tag color="grey">待改进 {maintenance.quality.total}</Tag>
      </div>
      {actions.length > 0 && <Table rowKey="key" variant="secondary" dataSource={actions} columns={[
        { title: "条目", dataIndex: "title" }, { title: "处理建议", dataIndex: "action" }, { title: "召回", dataIndex: "recalls" },
      ]} />}
    </Card>
  </>;
}
