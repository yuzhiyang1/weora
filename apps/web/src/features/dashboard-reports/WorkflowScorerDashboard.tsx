import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";

import { Banner, Button, Card, Empty, Modal, Select, Table } from "../../components/console";
import type { Scope } from "../knowledge/api";
import { errorMessage } from "../runs/api";
import {
  readWorkflowScores, uploadWorkflowScores, type WorkflowGroup, type WorkflowGroupBy,
  readWorkflowScoreSync, type WorkflowMeasure, type WorkflowScoreImportResult,
} from "./api";
import type { ReportPageProps } from "./DashboardReportsPage";
import { modeLabel, number, percent, ReportResult, ReportWindow, runtimeLabel, timestamp } from "./report-controls";
import { ScoreDistribution, ScoreMetrics, ScoreTrend } from "./scoring-sections";
import { WorkflowScoreSyncPanel } from "./WorkflowScoreSyncPanel";

const dimensions: Record<WorkflowGroupBy, string> = {
  tool: "工具", model: "模型", team_revision: "团队修订", definition: "智能体定义",
  role: "角色", project: "项目", tag: "标签", fingerprint: "指纹", skill: "Skill", stage: "判定阶段",
};
const deciderName: Record<string, string> = {
  judge: "评审模型", ci: "合并请求 CI", check: "评分器检查", protected: "受保护路径",
};
const roleOrigin = { step: "步骤角色", member: "成员角色", both: "步骤与成员角色" };
const unavailable = "未记录";
const perPage = 25;
const maxFailures = 1000;
const initialFilters = {
  days: "30", scorer: "", cohort: "", groupBy: "" as WorkflowGroupBy | "", group: "", limit: perPage,
};

function measureLabel({ source_hash, team_revision, run_mode, judge }: WorkflowMeasure) {
  const judgeLabel = judge.kind === "model"
    ? [runtimeLabel(judge.tool ?? unavailable), judge.model ?? "模型未记录",
      judge.model_source === "requested" ? "请求模型" : ""].filter(Boolean).join(" · ")
    : judge.kind === "human" ? "人工标注" : "阶段判定";
  return [
    "标准 " + source_hash.replace(/^sha256:/, "").slice(0, 12),
    team_revision ? "已发布 " + team_revision.slice(0, 10) : "未记录发布修订",
    modeLabel(run_mode ?? unavailable), judgeLabel,
  ].join(" · ");
}

function groupLabel(row: WorkflowGroup, by: WorkflowGroupBy) {
  if (row.kind !== "value" || !row.value) return row.kind === "none" ? "无" : unavailable;
  const value = by === "stage" ? (deciderName[row.value] ?? row.value) : by === "tool" ? runtimeLabel(row.value) : row.value;
  const short = by === "team_revision" || by === "fingerprint" ? value.slice(0, 12) : value;
  return [short, row.model_source === "requested" ? "请求模型" : "", row.local_changes ? "有本地变更" : "",
    row.role_source ? roleOrigin[row.role_source] : ""].filter(Boolean).join(" · ");
}

export function WorkflowScorerDashboard({ scope, scopeControl, user, canManageImprovements }: ReportPageProps) {
  const [filters, setFilters] = useState(initialFilters);
  const [uploading, setUploading] = useState(false);
  const [imported, setImported] = useState<WorkflowScoreImportResult>();
  const client = useQueryClient();
  const key = ["dashboard", "workflow-scores", scope.workspace, user, scope.project];
  const syncKey = ["dashboard", "workflow-score-sync", scope.workspace, user, scope.project];
  const sync = useQuery({
    queryKey: syncKey, queryFn: () => readWorkflowScoreSync(scope),
    refetchInterval: ({ state }) => {
      if (!state.data?.available || ["disabled", "unconfigured"].includes(state.data.status)) return false;
      return ["queued", "syncing"].includes(state.data.status) ? 5000 : 30000;
    },
  });
  const report = useQuery({
    // 新分支快照发布之后重读分组。失败时继续读取上次成功提交。
    queryKey: [...key, filters, sync.data?.version, sync.data?.revision],
    queryFn: () => readWorkflowScores(scope, {
      days: filters.days, limit: String(filters.limit),
      ...(filters.scorer ? { scorer: filters.scorer } : {}),
      ...(filters.cohort ? { cohort: filters.cohort } : {}),
      ...(filters.groupBy ? { group_by: filters.groupBy } : {}),
      ...(filters.group ? { group: filters.group } : {}),
    }),
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey.at(-2) === sync.data?.version && previousQuery?.queryKey.at(-1) === sync.data?.revision
        ? previous : undefined,
  });
  const selected = report.data?.selected;
  const measures = report.data?.measures ?? [];
  const names = Array.from(new Set(measures.map((item) => item.scorer)));
  const activeGroup = selected?.groups.find((row) => row.id === selected.active_group);

  function change(patch: Partial<typeof initialFilters>) {
    // 按照当前响应固定度量，避免增加失败明细条数或切换分组时选中其他标准。
    setFilters((previous) => ({
      ...previous, scorer: selected?.scorer ?? previous.scorer, cohort: selected?.id ?? previous.cohort,
      limit: perPage, ...patch,
    }));
  }

  function chooseGroup(by: WorkflowGroupBy, id: string) {
    change({ groupBy: by, group: filters.groupBy === by && filters.group === id ? "" : id });
  }

  return <div className="report-stack">
    <div className="report-toolbar">
      {scopeControl}
      <ReportWindow days={filters.days} options={[7, 30, 90, 180]} change={(days) => change({ days, group: "" })} />
      <Select aria-label="选择工作流评分器" value={selected?.scorer} disabled={!names.length || report.isPlaceholderData}
        optionList={names.map((value) => ({ value, label: value }))}
        onChange={(value) => change({ scorer: String(value), cohort: "", group: "" })} />
      {canManageImprovements && <Button onClick={() => setUploading(true)}>导入团队评分</Button>}
    </div>
    <WorkflowScoreSyncPanel scope={scope} user={user} status={sync.data} canConfigure={canManageImprovements}
      pending={sync.isPending} failed={!!sync.error} retry={() => void sync.refetch()}
      updated={(status) => client.setQueryData(syncKey, status)} />
    {imported && <Banner title="团队评分已导入" description={
      `新增 ${imported.recorded} 条，已存在 ${imported.unchanged} 条，跳过会话评分 ${imported.skipped_sessions} 条。`
    } />}
    {uploading && <WorkflowScoreImportDialog scope={scope} close={() => setUploading(false)} complete={(result) => {
      setImported(result);
      setUploading(false);
      void client.invalidateQueries({ queryKey: key });
    }} />}
    <ReportResult pending={report.isPending} error={report.error} retry={() => void report.refetch()}>
      {selected ? <div className="report-stack" aria-busy={report.isPlaceholderData}>
        <ScoreMetrics stats={selected.stats} />
        <Card title="评分口径">
          <Select aria-label="工作流评分口径" value={selected.id} disabled={report.isPlaceholderData}
            optionList={measures.filter((measure) => measure.scorer === selected.scorer)
              .map((measure) => ({ value: measure.id, label: measureLabel(measure) }))}
            onChange={(value) => change({ cohort: String(value), group: "" })} />
        </Card>
        <ScoreTrend weeks={selected.weeks} />
        <ScoreDistribution labels={selected.labels} />
        <Card title="这些标签由什么判定">
          <p className="report-muted">
            评审前已判定 {number(selected.before_judge)} / {number(selected.stats.total)}
            {" · "}评审模型通过率 {percent(selected.model_stats.pass_rate)}
            {selected.model_stats.total > 0 && `（${number(selected.model_stats.passed)} / ${number(selected.model_stats.total)}）`}
          </p>
          <GroupsTable rows={selected.stages} by="stage" stageSummary total={selected.stats.total}
            active={filters.groupBy === "stage" ? selected.active_group : null}
            choose={(id) => chooseGroup("stage", id)} disabled={report.isPlaceholderData} />
        </Card>
        <Card title="分组对比">
          <div className="report-toolbar">
            <Select aria-label="工作流评分分组依据" value={filters.groupBy || "all"} disabled={report.isPlaceholderData}
              optionList={[{ value: "all", label: "不分组" },
                ...Object.entries(dimensions).map(([value, label]) => ({ value, label }))]}
              onChange={(value) => change({ groupBy: value === "all" ? "" : value as WorkflowGroupBy, group: "" })} />
            {filters.group && <Button onClick={() => change({ group: "" })}>清除分组筛选</Button>}
          </div>
          {filters.groupBy && <GroupsTable rows={selected.groups} by={filters.groupBy} active={selected.active_group}
            choose={(id) => chooseGroup(filters.groupBy as WorkflowGroupBy, id)} disabled={report.isPlaceholderData} />}
        </Card>
        <Card title={"失败明细" + (activeGroup && filters.groupBy ? " · " + groupLabel(activeGroup, filters.groupBy) : "")}>
          {selected.failures.items.length ? <>
            <Table rowKey="id" variant="secondary" dataSource={selected.failures.items} columns={[
              { title: "标签", dataIndex: "label" },
              { title: "判定阶段", dataIndex: "decided_by", render: (value) => value ? deciderName[value] : unavailable },
              { title: "工作流运行", dataIndex: "run_id", render: (value) => <code title={value}>{value}</code> },
              { title: "步骤 / 轮次", key: "subject", render: (_, row) => row.step + " / " + number(row.attempt) },
              { title: "工具", dataIndex: "tool", render: (value) => value ? runtimeLabel(value) : unavailable },
              { title: "评分时间", dataIndex: "scored_at", render: timestamp },
            ]} />
            <div className="report-pagination">
              <span>已显示 {number(selected.failures.items.length)} / {number(selected.failures.total)} 条</span>
              {selected.failures.items.length < Math.min(selected.failures.total, maxFailures) &&
                <Button disabled={report.isPlaceholderData} onClick={() => change({ limit: filters.limit + perPage })}>显示更多</Button>}
              {selected.failures.items.length === maxFailures && selected.failures.total > maxFailures &&
                <span className="report-muted">可缩短时间窗口或选择分组查看其他标签</span>}
            </div>
          </> : <Empty title="当前筛选暂无失败标签" />}
        </Card>
      </div> : <Empty title="当前范围和时间窗口暂无工作流评分" />}
    </ReportResult>
  </div>;
}

function GroupsTable({ rows, by, active, choose, disabled, stageSummary = false, total = 0 }: {
  rows: WorkflowGroup[]; by: WorkflowGroupBy; active: string | null; choose: (value: string) => void;
  disabled: boolean; stageSummary?: boolean; total?: number;
}) {
  return <Table rowKey="id" variant="secondary" dataSource={rows} columns={[
    { title: stageSummary ? "判定来源" : dimensions[by], key: "group", render: (_, row) =>
      <Button size="small" theme={active === row.id ? "light" : "borderless"} aria-pressed={active === row.id}
        disabled={disabled} onClick={() => choose(row.id)}>
        <span title={row.value ?? undefined}>{groupLabel(row, by)}</span>
      </Button> },
    { title: "标签", key: "count", render: (_, row) => number(row.stats.total) },
    { title: "通过", key: "passed", render: (_, row) => number(row.stats.passed) },
    { title: "未通过", key: "failed", render: (_, row) => number(row.stats.failed) },
    { title: stageSummary ? "占比" : "通过率", key: "rate",
      render: (_, row) => percent(stageSummary && total ? row.stats.total / total : row.stats.pass_rate) },
  ]} />;
}

function WorkflowScoreImportDialog({ scope, close, complete }: {
  scope: Scope; close: () => void; complete: (result: WorkflowScoreImportResult) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File>();
  const [fileError, setFileError] = useState("");
  const upload = useMutation({
    mutationFn: async (selected: File) => uploadWorkflowScores(scope, await selected.text()),
    onSuccess: complete,
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (file && !upload.isPending) upload.mutate(file);
  }
  const error = fileError || (upload.error ? errorMessage(upload.error) : "");
  return <Modal visible title="导入团队评分" onCancel={close} maskClosable={!upload.isPending} closeOnEsc={!upload.isPending}>
    <form className="report-stack" onSubmit={submit}>
      <p>选择 TeamAI 团队报表分支中 <code>scores/&lt;成员&gt;/&lt;月份&gt;.jsonl</code>。记录导入到当前报表范围。</p>
      <input ref={input} type="file" hidden accept=".jsonl,.ndjson" disabled={upload.isPending}
        onChange={(event) => {
          const next = event.target.files?.[0];
          if (!next) return;
          // 失败后允许重新选择同一个文件。导入权限、文件格式和大小由接口再次校验。
          event.target.value = "";
          upload.reset();
          const invalid = next.size === 0 || next.size > 1024 * 1024;
          setFileError(invalid ? "请选择非空且不超过 1 MiB 的评分文件。" : "");
          setFile(invalid ? undefined : next);
        }} />
      <div className="report-inline">
        <Button onClick={() => input.current?.click()} disabled={upload.isPending}>选择 JSONL 文件</Button>
        {file && <span>{file.name}</span>}
      </div>
      {error && <Banner type="danger" description={error} />}
      <div className="modal-actions">
        <Button disabled={upload.isPending} onClick={close}>取消</Button>
        <Button htmlType="submit" theme="solid" loading={upload.isPending} disabled={!file}>导入</Button>
      </div>
    </form>
  </Modal>;
}
