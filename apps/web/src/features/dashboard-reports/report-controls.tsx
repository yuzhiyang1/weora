import { Card as HeroCard } from "@heroui/react";
import type { ReactNode } from "react";

import { Button, Empty, Select, Skeleton } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { errorMessage } from "../runs/api";

const numberFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const percentFormat = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });
const costFormat = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 4 });
const timestampFormat = new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" });

export const number = (value: number | null) => value === null ? "—" : numberFormat.format(value);
export const percent = (value: number | null) => value === null ? "—" : percentFormat.format(value);
export const cost = (micros: number | null) => micros === null ? "—" : costFormat.format(micros / 1_000_000);
export const timestamp = (value: string | null) => value ? timestampFormat.format(new Date(value)) : "—";
export const dayLabel = (value: string) => value.slice(5);
export const runtimeLabel = (runtime: string) => ({
  codex: "Codex", claude: "Claude Code", "claude-code": "Claude Code", human: "人工评审",
}[runtime] ?? runtime);
export const modeLabel = (mode: string) => ({
  attended: "有人值守", unattended: "无人值守", not_recorded: "未记录",
}[mode] ?? mode);
export const evidenceLabel = (evidence: string) => evidence === "local" ? "本机会话" : "共享会话";
export const gateLabel = { held: "通过门禁", incomplete: "证据不足", regressed: "检测到退化" };
export const runLabel = { complete: "已完成", expired: "已到期", pending: "运行中" };
export const armLabel = { main: "基线", pulled: "改动后" };
export const proposalLabel = { open: "开放", merged: "已合并", closed: "已关闭" };
export const gitStartLabel = (status: string) => ({
  recorded: "已记录", tracked_changes: "有本地变更", repository_unavailable: "仓库连接不可用",
  not_recorded: "缺少证据",
}[status] ?? status);

export function ReportWindow({ days, options, change }: { days: string; options: number[]; change: (days: string) => void }) {
  return <Select aria-label="统计时间窗口" value={days} onChange={(value) => change(String(value))}
    optionList={options.map((option) => ({ value: String(option), label: "近 " + option + " 天" }))} />;
}

export function ReportResult({ pending, error, retry, children }: {
  pending: boolean; error: unknown; retry: () => void; children: ReactNode;
}) {
  if (pending) return <Skeleton active />;
  if (error) return <AssetLoadError title="报表暂时无法加载" description={errorMessage(error)} onRetry={retry} />;
  return <>{children}</>;
}

export function Metrics({ items }: { items: { label: string; value: string; detail?: string }[] }) {
  return <div className="dashboard-metrics report-metrics">
    {items.map(({ label, value, detail }) => <HeroCard key={label} className="metric-card">
      <HeroCard.Header className="metric-label"><HeroCard.Title aria-level={2}>{label}</HeroCard.Title></HeroCard.Header>
      <HeroCard.Content><strong>{value}</strong>{detail && <p className="report-muted">{detail}</p>}</HeroCard.Content>
    </HeroCard>)}
  </div>;
}

export type ChartPoint = { id: string; label: string; values: (number | null)[] };

/** 无采样的点保留空位，真实 0 值仍带有可读标签。 */
export function ReportChart({ label, points, format = number, ceiling }: {
  label: string; points: ChartPoint[]; format?: (value: number | null) => string; ceiling?: number;
}) {
  const values = points.flatMap((point) => point.values).filter((value): value is number => value !== null);
  if (!values.length) return <Empty title="当前范围暂无可绘制的数据" />;
  const maximum = ceiling ?? Math.max(...values, 1);
  return <div className="report-chart-scroll">
    <ul className="report-chart" aria-label={label}>
      {points.map((point) => <li key={point.id} title={[point.label, ...point.values.map(format)].join(" · ")}>
        <div className="report-chart-slot" aria-hidden="true">
          {point.values.map((value, index) => <span key={index} data-missing={value === null || undefined}
            data-series={index} style={{ height: value === null ? "0" : Math.max(value / maximum * 100, 1) + "%" }} />)}
        </div>
        <span className="report-chart-label">{point.label}</span>
        <span className="sr-only">{point.values.map(format).join("、")}</span>
      </li>)}
    </ul>
  </div>;
}

export function ReportPagination({ offset, next, total, count, change }: {
  offset: number; next: number | null; total: number; count: number; change: (offset: number) => void;
}) {
  if (!offset && next === null) return null;
  const limit = next === null ? 20 : next - offset;
  return <div className="report-pagination">
    <span>{count ? (offset + 1) + "–" + (offset + count) + " / " + total : "共 " + total + " 条"}</span>
    <Button size="small" disabled={!offset} onClick={() => change(Math.max(0, offset - limit))}>上一页</Button>
    <Button size="small" disabled={next === null} onClick={() => { if (next !== null) change(next); }}>下一页</Button>
  </div>;
}
