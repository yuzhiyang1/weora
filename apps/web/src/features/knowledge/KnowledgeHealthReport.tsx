import { Card } from "@heroui/react";

import { Empty, Table, Tag, Typography } from "../../components/console";
import type { HealthEntry, HealthReport } from "./api";

const actions = { promote: "可晋升", prune: "建议归档", quality: "待改进" } as const;

/** 报告总量按完整范围统计，各明细表使用服务端返回的上限。 */
export function KnowledgeHealthReport({ report, labels }: { report: HealthReport; labels: Record<string, string> }) {
  const { summary, graph, code_sources: sources } = report;
  const metrics = [
    { label: "已发布知识", value: summary.entries },
    { label: "召回次数", value: summary.recalls },
    { label: "采纳次数", value: summary.upvotes },
    { label: "召回覆盖率", value: `${summary.coverage}%` },
  ];
  const maintenance = (Object.keys(actions) as (keyof typeof actions)[]).flatMap((action) =>
    report.maintenance[action].items.map((entry) => ({ ...entry, action: actions[action], key: `${action}:${entry.id}` })),
  );
  const entries = [
    ...report.top_recalled.map((entry) => ({ ...entry, group: "已召回" })),
    ...report.silent.map((entry) => ({ ...entry, group: "未召回" })),
  ];
  return <div className="knowledge-health">
    <div className="dashboard-metrics knowledge-health-metrics">
      {metrics.map(({ label, value }) => <Card key={label} className="metric-card">
        <Card.Header className="metric-label"><Card.Title aria-level={3}>{label}</Card.Title></Card.Header>
        <Card.Content><strong>{value}</strong></Card.Content>
      </Card>)}
    </div>
    {summary.entries === 0 ? <Empty title="当前范围没有已发布知识" /> : <>
      <section>
        <Typography.Title heading={3}>按类型查看覆盖率</Typography.Title>
        <Table className="asset-stack-table" rowKey="kind" variant="secondary" dataSource={report.coverage} columns={[
          { title: "类型", dataIndex: "kind", render: (kind: string) => labels[kind] ?? kind },
          { title: "条目数", dataIndex: "total" },
          { title: "已召回", dataIndex: "covered" },
          { title: "覆盖率", dataIndex: "percent", render: (percent: number) => `${percent}%` },
        ]} />
      </section>
      <section>
        <Typography.Title heading={3}>维护候选</Typography.Title>
        <ul className="knowledge-report-totals">
          {(Object.keys(actions) as (keyof typeof actions)[]).map((action) => (
            <li key={action}>{actions[action]} <strong>{report.maintenance[action].total}</strong></li>
          ))}
        </ul>
        {maintenance.length > 0 && <Table className="asset-stack-table" rowKey="key" variant="secondary"
          dataSource={maintenance} columns={[
            { title: "名称", dataIndex: "title", render: (title: string) => <span className="knowledge-text">{title}</span> },
            { title: "类型", dataIndex: "kind", render: (kind: string) => labels[kind] ?? kind },
            { title: "置信度", dataIndex: "confidence", render: (value: number) => value.toFixed(2) },
            { title: "处理建议", dataIndex: "action" },
          ]} />}
      </section>
      <section>
        <Typography.Title heading={3}>召回明细</Typography.Title>
        <ul className="knowledge-report-totals">
          <li>热门 <strong>{summary.hot}</strong></li><li>低活跃 <strong>{summary.cold}</strong></li>
          <li>未召回 <strong>{summary.silent}</strong></li>
        </ul>
        <Table<HealthEntry & { group: string }> className="asset-stack-table" rowKey="id" variant="secondary"
          dataSource={entries} columns={[
            { title: "名称", dataIndex: "title", render: (title) => <span className="knowledge-text">{title}</span> },
            { title: "状态", dataIndex: "group" },
            { title: "召回", dataIndex: "recalls" },
            { title: "采纳", dataIndex: "upvotes" },
          ]} />
      </section>
      <section>
        <Typography.Title heading={3}>知识关系</Typography.Title>
        <ul className="knowledge-report-totals">
          <li>有效关系 <strong>{graph.relations}</strong></li><li>未解析 <strong>{graph.unresolved}</strong></li>
          <li>无关系条目 <strong>{graph.without_relations}</strong></li>
          <li>未召回且无关系 <strong>{graph.silent_without_relations}</strong></li>
        </ul>
      </section>
      <div className="knowledge-health-columns">
        <section>
          <Typography.Title heading={3}>最近召回月份</Typography.Title>
          {report.last_recall_months.length ? <Table className="asset-stack-table" rowKey="month" variant="secondary"
            dataSource={report.last_recall_months} columns={[
              { title: "月份", dataIndex: "month" }, { title: "条目数", dataIndex: "entries" },
            ]} /> : <p>当前范围暂无召回记录。</p>}
        </section>
        <section>
          <Typography.Title heading={3}>贡献成员（{summary.contributors}）</Typography.Title>
          <Table className="asset-stack-table" rowKey="id" variant="secondary" dataSource={report.authors} columns={[
            { title: "成员", dataIndex: "name" }, { title: "贡献条目", dataIndex: "entries" },
            { title: "条目被召回", dataIndex: "recalls" },
          ]} />
        </section>
      </div>
    </>}
    {sources.length > 0 && <section>
      <Typography.Title heading={3}>代码来源</Typography.Title>
      <Table className="asset-stack-table" rowKey="id" variant="secondary" dataSource={sources} columns={[
        { title: "来源", dataIndex: "name", render: (name: string) => <span className="knowledge-text">{name}</span> },
        { title: "类型", dataIndex: "source", render: (source: string) => source === "repository" ? "登记仓库" : "本地目录" },
        { title: "已索引文件", dataIndex: "indexed" },
        { title: "新鲜度", render: (_, row) => !row.revision ? <Tag color="grey">未索引</Tag>
          : row.freshness === "current" ? <Tag color="green">最新</Tag>
            : row.freshness === "stale" ? <Tag color="orange">已过期</Tag> : <Tag color="grey">未知</Tag> },
      ]} />
    </section>}
  </div>;
}
