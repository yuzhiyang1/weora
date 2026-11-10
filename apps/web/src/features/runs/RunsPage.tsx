import type { TableColumn } from "../../components/console/data-table";
import {
  ArrowLeft,
  Plus as IconPlus,
  RefreshCw as IconRefresh,
} from "lucide-react";
import { buttonVariants } from "@heroui/styles";
import {
  Banner,
  Button,
  Empty,
  Input,
  Table,
  Typography,
} from "../../components/console";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { formatDate } from "../skills/SkillsPage";
import { CreateRunDialog } from "./CreateRunDialog";
import { errorMessage, listRuns, runKey, type RunSummary } from "./api";
import { RunStatusTag } from "./RunStatusTag";
import "./runs.css";

export function RunsPage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("运行页面缺少当前身份");
  const { workspace, user } = principal;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const [statusFilter, setStatusFilter] = useState("all");
  const [keyword, setKeyword] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  // 直接从路由派生创建状态，避免导航后在 effect 中重复设置本地状态。
  const creating = open || searchParams.get("new") === "1";
  function closeCreate() {
    setOpen(false);
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("new");
        return next;
      },
      { replace: true },
    );
  }
  const query = useInfiniteQuery({
    queryKey: runKey(workspace.id, user.id),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listRuns(workspace.id, pageParam),
    getNextPageParam: (page) => page.next_before_id ?? undefined,
    refetchInterval: 5000,
  });
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  const displayed = items.filter(
    (item) =>
      (statusFilter === "all" ||
        (statusFilter === "active"
          ? [
              "QUEUED",
              "RUNNING",
              "WAITING_CONTEXT",
              "WAITING_APPROVAL",
              "RECOVERY_REQUIRED",
            ].includes(item.status)
          : statusFilter === "attention"
            ? [
                "WAITING_CONTEXT",
                "WAITING_APPROVAL",
                "RECOVERY_REQUIRED",
              ].includes(item.status)
            : item.status === statusFilter)) &&
      `${item.task_title ?? ""} ${item.agent_name} ${item.id} ${item.agent_version}`
        .toLowerCase()
        .includes(keyword.toLowerCase()),
  );
  const columns: TableColumn<RunSummary>[] = [
    {
      title: "任务",
      dataIndex: "task_title",
      render: (_: unknown, run: RunSummary) => (
        <Link className="run-link" to={`/runs/${run.id}`}>
          <strong>{run.task_title || "未命名任务"}</strong>
          <small>
            {run.agent_name} · 固定版本 {run.agent_version}
          </small>
        </Link>
      ),
    },
    {
      title: "执行状态",
      dataIndex: "status",
      width: 170,
      render: (_: unknown, run: RunSummary) => (
        <RunStatusTag status={run.status} />
      ),
    },
    { title: "记录序号", dataIndex: "last_sequence", width: 110 },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 170,
      render: (date: string) => <time dateTime={date}>{formatDate(date)}</time>,
    },
    {
      title: "",
      dataIndex: "id",
      width: 90,
      render: (id: string) => (
        <Link
          className={buttonVariants({ variant: "outline", size: "sm" })}
          to={`/runs/${id}`}
        >
          查看任务
        </Link>
      ),
    },
  ];
  return (
    <section className="asset-page runs-list-page page-enter">
      <Link
        to="/"
        className={`${buttonVariants({ variant: "outline" })} runs-home-link`}
      >
        <ArrowLeft size={16} aria-hidden="true" />
        返回工作台
      </Link>
      <header className="page-header">
        <div>
          <span className="page-eyebrow">RUNS / PRIVATE CONSOLE</span>
          <Typography.Title heading={1}>任务列表</Typography.Title>
          <Typography.Paragraph>
            提交目标，查看真实回答、工具证据和可续读运行记录。
          </Typography.Paragraph>
        </div>
        <div className="run-actions">
          <Button
            aria-label="刷新运行"
            icon={<IconRefresh />}
            onClick={() => void query.refetch()}
          >
            刷新
          </Button>
          <Button
            aria-label="发起任务"
            icon={<IconPlus />}
            onClick={() => setOpen(true)}
            size="large"
            theme="solid"
            type="primary"
          >
            发起任务
          </Button>
        </div>
      </header>
      <Banner
        className="boundary-banner"
        closeIcon={null}
        title="你的私有任务"
        description="仅展示当前工作空间中由你发起的任务。执行状态与渠道投递状态分别记录；排队不代表模型已连接。"
        type="info"
      />
      <section className="asset-surface">
        <div className="task-toolbar">
          <div className="task-filters" aria-label="任务状态筛选">
            {[
              ["all", "全部"],
              ["active", "进行中"],
              ["attention", "待处理"],
              ["SUCCEEDED", "已完成"],
              ["FAILED", "失败"],
            ].map(([key, label]) => (
              <Button
                theme={statusFilter === key ? "light" : "borderless"}
                size="small"
                key={key}
                aria-pressed={statusFilter === key}
                onClick={() => setStatusFilter(key!)}
              >
                {label}
              </Button>
            ))}
          </div>
          <Input
            ref={searchInput}
            className="task-search"
            type="search"
            aria-label="搜索运行"
            placeholder="搜索任务标题、Agent、版本或任务 ID…"
            value={keyword}
            onChange={setKeyword}
          />
        </div>
        {(statusFilter !== "all" || keyword) && (
          <p className="filter-scope">
            筛选当前已加载的 {items.length} 项任务；更早记录可继续加载。
          </p>
        )}
        {query.isError ? (
          <Banner
            closeIcon={null}
            title="运行暂时无法加载"
            description={
              <div className="run-list-feedback">
                <p>{errorMessage(query.error)}</p>
                <Button size="small" onClick={() => void query.refetch()}>
                  重新加载
                </Button>
              </div>
            }
            type="danger"
          />
        ) : null}
        {!query.isPending && !query.isError && !items.length ? (
          <div className="asset-empty">
            <Empty
              title="还没有运行记录"
              description="发布一个 Agent 版本后，在这里发起第一项任务。"
            />
            <Button onClick={() => setOpen(true)}>发起第一个任务</Button>
          </div>
        ) : null}
        {query.isPending || displayed.length ? (
          <Table
            className="asset-table runs-list-table"
            variant="secondary"
            columns={columns}
            dataSource={displayed}
            loading={query.isPending}
            rowKey="id"
            pagination={false}
          />
        ) : null}
        {!query.isPending &&
          !query.isError &&
          items.length > 0 &&
          !displayed.length && (
            <div className="run-list-no-match">
              <Empty
                title="没有匹配的任务"
                description="调整关键词或状态筛选后再试。"
              />
              <Button
                onClick={() => {
                  setStatusFilter("all");
                  setKeyword("");
                  searchInput.current?.focus();
                }}
              >
                清除筛选
              </Button>
            </div>
          )}
        {query.hasNextPage ? (
          <div className="run-pagination">
            <Button
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              读取更早任务
            </Button>
          </div>
        ) : null}
      </section>
      <CreateRunDialog
        workspaceId={workspace.id}
        userId={user.id}
        open={creating}
        onClose={closeCreate}
        onCreated={(run) => {
          queryClient.setQueryData(runKey(workspace.id, user.id, run.id), run);
          void queryClient.invalidateQueries({
            queryKey: runKey(workspace.id, user.id),
            exact: true,
          });
          setOpen(false);
          navigate(`/runs/${run.id}`);
        }}
      />
    </section>
  );
}
