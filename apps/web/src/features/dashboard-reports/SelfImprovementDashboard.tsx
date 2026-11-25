import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Button, Card, Empty, Modal, Select, Table, Tag } from "../../components/console";
import { readFailureCandidates, readProposals, type Proposal } from "./api";
import type { ReportPageProps, ReportProps } from "./DashboardReportsPage";
import {
  evidenceLabel, gateLabel, gitStartLabel, Metrics, modeLabel, number, percent, proposalLabel,
  ReportPagination, ReportResult, runtimeLabel, timestamp,
} from "./report-controls";

function proofLabel(proposal: Proposal) {
  if (!proposal.proof) return "待验证";
  return proposal.proof.verified ? "已验证" : gateLabel[proposal.proof.gate];
}

export function SelfImprovementDashboard({ scope, user, scopeControl }: ReportPageProps) {
  const [params, setParams] = useSearchParams();
  const scorer = params.get("scorer") ?? "";
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string>();
  const key = ["dashboard", "self-improvement", scope.workspace, user, scope.project];
  const baseline = useQuery({ queryKey: [...key, "", 0], queryFn: () => readProposals(scope, "", 0) });
  const filtered = useQuery({
    queryKey: [...key, scorer, offset], queryFn: () => readProposals(scope, scorer, offset), enabled: !!scorer || offset > 0,
  });
  const report = scorer || offset ? filtered : baseline;
  const choices = Array.from(new Set([...(baseline.data?.scorers ?? []), ...(scorer ? [scorer] : [])]));
  const data = report.data;
  const active = data?.items.find((row) => row.id === selected);
  // 失败候选使用当前选择的具体评分器，跟踪页本身仍能显示项目的全部提案。
  const [candidateScorer, setCandidateScorer] = useState("");
  const subjectScorer = scorer || (choices.includes(candidateScorer) ? candidateScorer : choices[0] ?? "");

  function changeScorer(value: string) {
    setSelected(undefined);
    setOffset(0);
    setParams({ project: scope.project, ...(value ? { scorer: value } : {}) }, { replace: true });
  }

  return <div className="report-stack">
    <div className="report-toolbar">
      {scopeControl}
      <Select aria-label="提案评分器" value={scorer || "all"} onChange={(value) => changeScorer(value === "all" ? "" : String(value))}
        optionList={[{ value: "all", label: "全部评分器" }, ...choices.map((value) => ({ value, label: value }))]} />
      <Button theme="solid" disabled={report.isFetching} onClick={() => void report.refetch()}>刷新报告</Button>
    </div>
    <ReportResult pending={report.isPending} error={report.error} retry={() => void report.refetch()}>
      {data && <>
        <Metrics items={[
          { label: "失败标签", value: number(data.summary.failed_labels), detail: "近 30 天" },
          { label: "回归任务", value: number(data.summary.tasks), detail: "当前范围" },
          { label: "开放提案", value: number(data.summary.open), detail: "当前范围" },
          { label: "已合并", value: number(data.summary.merged), detail: "已登记交付" },
        ]} />
        <Card title="提案跟踪">
          <div className="report-stack">
            <div className="report-inline">
              <Tag color="blue">开放 {data.summary.open}</Tag>
              <Tag color="green">已合并 {data.summary.merged}</Tag>
              <Tag color="grey">已关闭 {data.summary.closed}</Tag>
              <span className="report-muted">本页已验证 {data.items.filter((row) => row.proof?.verified).length}</span>
            </div>
            {data.items.length ? <Table rowKey="id" variant="secondary" dataSource={data.items} columns={[
              { title: "提案", key: "open", render: (_, row) => <Button theme="borderless" size="small"
                onClick={() => setSelected(row.id)}>{row.reason || row.suite}</Button> },
              { title: "状态", dataIndex: "status", render: (value) => proposalLabel[value] },
              { title: "评分器", dataIndex: "scorer" },
              { title: "验证", key: "proof", render: (_, row) => proofLabel(row) },
              { title: "交付", dataIndex: "pr_number", render: (value) => value === null ? "—" : "PR #" + value },
            ]} /> : <Empty title="当前筛选暂无改进提案" />}
          </div>
          <ReportPagination offset={offset} next={data.next_offset} total={data.total} count={data.items.length} change={setOffset} />
        </Card>
        <Card title="失败候选">
          {!scorer && choices.length > 0 && <Select aria-label="失败候选评分器" value={subjectScorer}
            onChange={(value) => setCandidateScorer(String(value))}
            optionList={choices.map((value) => ({ value, label: value }))} />}
          {subjectScorer ? <FailureCandidateList scope={scope} user={user} scorer={subjectScorer} /> :
            <Empty title="当前项目暂无可用于回归的评分器" />}
        </Card>
        <Card title="合并后效果">
          {data.items.some((row) => row.effect?.status === "measured") ?
            <Table rowKey="id" variant="secondary" dataSource={data.items.filter((row) => row.effect?.status === "measured")} columns={[
              { title: "提案", dataIndex: "reason" }, { title: "合并时间", dataIndex: "settled_at", render: timestamp },
              { title: "可比较口径", key: "effect", render: (_, row) =>
                row.effect?.cohorts.filter((effect) => effect.before.total > 0 && effect.after.total > 0).length },
              { title: "明细", key: "open", render: (_, row) => <Button size="small" onClick={() => setSelected(row.id)}>查看效果</Button> },
            ]} /> : <Empty title="本页提案暂无可比较的合并前后样本" />}
        </Card>
      </>}
    </ReportResult>
    {active && <ProposalDetail proposal={active} project={scope.project} close={() => setSelected(undefined)} />}
  </div>;
}

function FailureCandidateList({ scope, user, scorer }: ReportProps & { scorer: string }) {
  const query = useQuery({
    queryKey: ["dashboard", "candidates", scope.workspace, user, scope.project, scorer],
    queryFn: () => readFailureCandidates(scope, scorer),
  });
  return <ReportResult pending={query.isPending} error={query.error} retry={() => void query.refetch()}>
    <Table rowKey="subject_id" variant="secondary" dataSource={query.data?.subjects ?? []} columns={[
      { title: "失败标签", dataIndex: "latest", render: (value) => value.label },
      { title: "工具", key: "tool", render: (_, row) => runtimeLabel(row.latest.runtime) },
      { title: "回归任务", dataIndex: "task", render: (value) => value ?? "待补齐" },
      { title: "启动证据", key: "git", render: (_, row) => gitStartLabel(
        (row.recorded?.git_start ?? row.latest.git_start).status,
      ) },
      { title: "相关经验", dataIndex: "learnings", render: (items) => items.map((item) => item.title).join("、") || "—" },
    ]} />
  </ReportResult>;
}

function ProposalDetail({ proposal, project, close }: { proposal: Proposal; project: string; close: () => void }) {
  const proof = proposal.proof;
  const link = proof && "/benchmarks?" + new URLSearchParams({ project, suite: proposal.suite_id, run: proof.run_id }).toString();
  const effects = (proposal.effect?.cohorts ?? []).filter((row) => row.before.total && row.after.total).map((row) => ({
    ...row, id: JSON.stringify([row.scorer_revision, row.runtime, row.evidence, row.run_mode, row.driver, row.requested_model]),
    label: ["标准 v" + row.scorer_revision, runtimeLabel(row.runtime), evidenceLabel(row.evidence),
      modeLabel(row.run_mode), runtimeLabel(row.driver), row.requested_model].filter(Boolean).join(" · "),
  }));
  return <Modal visible title="改进提案" onCancel={close} width={820}>
    <div className="report-detail">
      <p>{proposal.reason ?? "该提案暂无资源修订"}</p>
      <p>{proposal.scorer} · {proposalLabel[proposal.status]} · {timestamp(proposal.created_at)}</p>
      {proposal.revision !== null && <p>修订 {proposal.revision} · {number(proposal.tasks)} 个回归任务</p>}
      {proposal.resources.length > 0 && <ul>{proposal.resources.map((path) => <li key={path}><code>{path}</code></li>)}</ul>}
      {proposal.pr_number !== null && <p>交付 PR #{proposal.pr_number}</p>}
      {proposal.ci && <p>CI 运行 {proposal.ci.run}{proposal.ci.attempt ? " · 第 " + proposal.ci.attempt + " 次尝试" : ""}</p>}
      {proposal.merge_commit && <p>合并提交 <code>{proposal.merge_commit}</code></p>}
      {link && <Link to={link}>查看基准证明 · {proofLabel(proposal)}</Link>}
      {effects.length > 0 && <Card title="合并后效果">
        <Table rowKey="id" variant="secondary" dataSource={effects} columns={[
          { title: "口径", dataIndex: "label" },
          { title: "合并前", dataIndex: "before", render: (value) => percent(value.pass_rate) + " · " + number(value.total) + " 条" },
          { title: "合并后", dataIndex: "after", render: (value) => percent(value.pass_rate) + " · " + number(value.total) + " 条" },
        ]} />
      </Card>}
    </div>
  </Modal>;
}
