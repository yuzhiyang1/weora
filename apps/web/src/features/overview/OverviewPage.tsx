import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  Bot,
  CircleCheck,
  Clock3,
  FolderGit2,
  Plus,
  Puzzle,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Banner } from "../../components/console";
import { buttonVariants } from "@heroui/styles";
import { Card, Chip, Skeleton } from "@heroui/react";
import { listAgents } from "../agents/api";
import { useAuth } from "../auth/AuthProvider";
import { listProjects } from "../projects/api";
import { listRuns, runTaskTitle } from "../runs/api";
import { RunStatusTag } from "../runs/RunStatusTag";
import { listSkills } from "../skills/api";
import { formatDate } from "../skills/SkillsPage";

/** 概览只聚合当前账户可读取的数据，不展示样例或把当前页数量当作全量统计。 */
export function OverviewPage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("工作台缺少当前身份");
  const { workspace, user } = principal;
  const runs = useQuery({
    queryKey: ["overview", workspace.id, user.id, "runs"],
    queryFn: () => listRuns(workspace.id),
    refetchInterval: 10000,
  });
  const agents = useQuery({
    queryKey: ["overview", workspace.id, user.id, "agents"],
    queryFn: () => listAgents(workspace.id),
  });
  const skills = useQuery({
    queryKey: ["overview", workspace.id, user.id, "skills"],
    queryFn: () => listSkills(workspace.id, {}),
  });
  const projects = useQuery({
    queryKey: ["overview", workspace.id, user.id, "projects"],
    queryFn: () => listProjects(workspace.id),
  });
  const recent = runs.data?.items ?? [];
  const pending = recent.filter((run) =>
    ["WAITING_APPROVAL", "RECOVERY_REQUIRED", "WAITING_CONTEXT"].includes(
      run.status,
    ),
  );
  const metrics = [
    {
      label: "近期任务",
      value: runs.data?.items.length,
      pending: runs.isPending,
      icon: Activity,
      link: "/runs",
      note: "最近一页可见任务",
    },
    {
      label: "Agent",
      value: agents.data?.items.length,
      pending: agents.isPending,
      icon: Bot,
      link: "/agents",
      note: "已加载的团队 Agent",
    },
    {
      label: "Skills",
      value: skills.data?.items.length,
      pending: skills.isPending,
      icon: Puzzle,
      link: "/skills",
      note: "已加载的能力资产",
    },
    {
      label: "项目",
      value: projects.data?.items.length,
      pending: projects.isPending,
      icon: FolderGit2,
      link: "/projects",
      note: "已加载的团队项目",
    },
  ];
  const published =
    agents.data?.items.filter(
      (agent) => agent.status === "active" && agent.latest_published_version,
    ) ?? [];
  const mobileTaskAction = (
    <span
      className={`${buttonVariants({ variant: "outline", size: "sm" })} dashboard-open-task`}
      aria-hidden="true"
    >
      查看任务
    </span>
  );
  return (
    <section className="dashboard-page page-enter">
      <header className="page-header dashboard-heading">
        <div>
          <span className="page-eyebrow">WORKSPACE OVERVIEW</span>
          <h1>工作台</h1>
          <p>
            {user.display_name}
            ，欢迎回来。查看近期任务、处理协作事项，或发起新任务。
          </p>
        </div>
        <Link
          className={buttonVariants({ variant: "primary" })}
          to="/runs?new=1"
        >
          <Plus size={16} />
          发起任务
        </Link>
      </header>
      <div className="dashboard-metrics">
        {metrics.map(({ icon: Icon, ...metric }) => (
          <Link className="metric-link" key={metric.label} to={metric.link}>
            <Card className="metric-card">
              <Card.Header className="metric-label">
                <Card.Title aria-level={2}>{metric.label}</Card.Title>
                <Icon size={20} aria-hidden="true" />
              </Card.Header>
              <Card.Content>
                <strong>
                  {metric.pending ? (
                    <Skeleton className="h-10 w-12" />
                  ) : (
                    (metric.value ?? "—")
                  )}
                </strong>
              </Card.Content>
              <Card.Footer className="metric-note">
                <small>{metric.note}</small>
                <ArrowUpRight size={16} aria-hidden="true" />
              </Card.Footer>
            </Card>
          </Link>
        ))}
      </div>
      {[runs, agents, skills, projects].some((query) => query.isError) && (
        <Banner
          type="warning"
          title="部分数据暂时无法加载"
          description="已加载的内容仍可使用，请进入对应页面重试。"
        />
      )}
      <div className="dashboard-columns">
        <Card
          className="dashboard-panel attention-panel"
          role="region"
          aria-label="需要你处理"
        >
          <Card.Header className="panel-heading">
            <Card.Title aria-level={2}>
              <Clock3 size={20} aria-hidden="true" />
              需要你处理
            </Card.Title>
            {/* 未完成查询时不把未知状态表示成零，避免误导用户。 */}
            <Chip size="sm" variant="soft">
              {runs.isPending || runs.isError ? "—" : pending.length}
            </Chip>
          </Card.Header>
          <Card.Content>
            {pending.length ? (
              pending.slice(0, 3).map((run) => (
                <Link
                  className="attention-row"
                  to={`/runs/${run.id}`}
                  key={run.id}
                >
                  <strong>{runTaskTitle(run)}</strong>
                  <RunStatusTag status={run.status} />
                  {mobileTaskAction}
                </Link>
              ))
            ) : (
              <div className="attention-empty">
                {runs.isPending || runs.isError ? (
                  <Clock3 size={20} aria-hidden="true" />
                ) : (
                  <CircleCheck
                    className="attention-clear"
                    size={20}
                    aria-hidden="true"
                  />
                )}
                <div>
                  <strong>
                    {runs.isPending
                      ? "正在检查近期任务"
                      : runs.isError
                        ? "暂时无法确认待处理状态"
                        : "近期任务暂无待处理事项"}
                  </strong>
                  <p>需要审批或补充上下文时，会在这里提醒你。</p>
                </div>
              </div>
            )}
          </Card.Content>
        </Card>
        <Card className="dashboard-panel dashboard-recent">
          <Card.Header className="panel-heading">
            <Card.Title aria-level={2}>
              <Activity size={20} aria-hidden="true" />
              最近任务
            </Card.Title>
            <Link
              className={buttonVariants({ variant: "ghost", size: "sm" })}
              to="/runs"
            >
              全部任务 <ArrowUpRight size={16} aria-hidden="true" />
            </Link>
          </Card.Header>
          <Card.Content>
            {runs.isPending ? (
              <div className="dashboard-skeleton">
                <Skeleton className="h-12 w-full" />
                <Skeleton className="h-12 w-full" />
                <Skeleton className="h-12 w-full" />
              </div>
            ) : recent.length ? (
              <div className="dashboard-task-list">
                {recent.slice(0, 6).map((run) => (
                  <Link
                    to={`/runs/${run.id}`}
                    className="dashboard-task-row"
                    key={run.id}
                  >
                    <span className="task-agent-icon">
                      <Bot size={17} />
                    </span>
                    <span>
                      <strong>{runTaskTitle(run)}</strong>
                      <small>
                        {run.agent_name} · 版本 {run.agent_version} · {formatDate(run.created_at)}
                      </small>
                    </span>
                    <RunStatusTag status={run.status} />
                    <ArrowRight size={14} aria-hidden="true" />
                    {mobileTaskAction}
                  </Link>
                ))}
              </div>
            ) : (
              <div className="dashboard-empty">
                <Activity size={25} />
                <h3>
                  {runs.isError ? "任务列表加载失败" : "第一项任务，从这里开始"}
                </h3>
                <p>
                  {runs.isError
                    ? "请进入任务列表重试，已保存的任务不会丢失。"
                    : "选择一个已发布 Agent，描述目标，持续查看进展。"}
                </p>
                <Link
                  className={buttonVariants({ variant: "outline", size: "sm" })}
                  to={runs.isError ? "/runs" : "/runs?new=1"}
                >
                  {runs.isError ? "查看任务" : "发起任务"}{" "}
                  <ArrowRight size={16} aria-hidden="true" />
                </Link>
              </div>
            )}
          </Card.Content>
        </Card>
        <Card className="dashboard-panel dashboard-agents">
          <Card.Header className="panel-heading">
            <Card.Title aria-level={2}>
              <Bot size={20} aria-hidden="true" />
              可用 Agent
            </Card.Title>
            <Link
              className={buttonVariants({ variant: "ghost", size: "sm" })}
              to="/agents"
            >
              管理 <ArrowUpRight size={16} aria-hidden="true" />
            </Link>
          </Card.Header>
          <Card.Content>
            {published.length ? (
              published.slice(0, 3).map((agent) => (
                <Link
                  className="available-agent"
                  to={`/agents/${agent.id}`}
                  key={agent.id}
                >
                  <span className="task-agent-icon">
                    <Bot size={17} />
                  </span>
                  <span>
                    <strong>{agent.display_name}</strong>
                    <small>{agent.latest_published_version}</small>
                  </span>
                  <ArrowRight size={14} />
                </Link>
              ))
            ) : (
              <div className="agent-setup">
                <p>
                  {agents.isPending
                    ? "正在读取已发布版本…"
                    : agents.isError
                      ? "Agent 加载失败，请前往 Agent 页面重试。"
                      : "发布 Agent 版本后，即可开始执行任务。"}
                </p>
                <Link to="/agents">
                  前往 Agent <ArrowRight size={14} />
                </Link>
              </div>
            )}
          </Card.Content>
        </Card>
      </div>
    </section>
  );
}
