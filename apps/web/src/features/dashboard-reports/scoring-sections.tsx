import { Card, Table } from "../../components/console";
import type { Counts, ScoreMeasure } from "./api";
import { dayLabel, Metrics, number, percent, ReportChart } from "./report-controls";

export function ScoreMetrics({ stats }: { stats: Counts }) {
  return <Metrics items={[
    { label: "通过率", value: percent(stats.pass_rate) },
    { label: "标签", value: number(stats.total) },
    { label: "通过", value: number(stats.passed) },
    { label: "未通过", value: number(stats.failed) },
  ]} />;
}

export function ScoreTrend({ weeks }: Pick<ScoreMeasure, "weeks">) {
  return <Card title="每周通过率">
    <ReportChart label="每周通过率" points={weeks.map((week) => ({
      id: week.week, label: dayLabel(week.week), values: [week.pass_rate],
    }))} format={percent} ceiling={1} />
    <details className="report-details">
      <summary>查看每周明细</summary>
      <Table rowKey="week" variant="secondary" dataSource={weeks} columns={[
        { title: "周起始日期", dataIndex: "week" }, { title: "标签", dataIndex: "total" },
        { title: "通过", dataIndex: "passed" }, { title: "通过率", dataIndex: "pass_rate", render: percent },
      ]} />
    </details>
  </Card>;
}

export function ScoreDistribution({ labels }: Pick<ScoreMeasure, "labels">) {
  // 导入模型以标签及分数组合保存历史判定，同名标签也可以完整展示。
  const rows = labels.map((label) => ({ ...label, id: JSON.stringify([label.value, label.score]) }));
  return <Card title="标签分布">
    <Table rowKey="id" variant="secondary" dataSource={rows} columns={[
      { title: "标签", dataIndex: "value" }, { title: "分数", dataIndex: "score" }, { title: "数量", dataIndex: "count" },
    ]} />
  </Card>;
}
