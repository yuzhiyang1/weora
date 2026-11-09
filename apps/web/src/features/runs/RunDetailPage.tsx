import {
  Banner,
  Button,
  Modal,
  Spin,
  TabPane,
  Tabs,
  Typography,
} from "../../components/console";
import { buttonVariants } from "@heroui/styles";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { RunContextPanel } from "../context/RunContextPanel";
import { RunChannelPanel } from "../context/RunChannelPanel";
import { formatDate } from "../skills/SkillsPage";
import {
  cancelRun,
  ConsoleApiError,
  errorMessage,
  getRun,
  getRunEvents,
  isTerminal,
  recoverRun,
  runKey,
  type RunEvent,
} from "./api";
import { RunStatusTag } from "./RunStatusTag";
import { DeliveryPanel } from "./DeliveryPanel";
import { ComputerPanel } from "./ComputerPanel";
import { RunReport } from "./RunReport";
import { ToolInvocationsPanel } from "./ToolInvocationsPanel";
import "./runs.css";
import { RunProjectPanel } from "../projects/RunProjectPanel";

export function RunDetailPage() {
  const { principal } = useAuth();
  const { runId } = useParams();
  if (!principal || !runId) throw new Error("运行详情缺少当前身份或任务");
  // 切换任务或账号时重建订阅和局部记录，不能沿用上一任务的私有正文。
  return (
    <RunDetailContent
      key={`${principal.workspace.id}:${principal.user.id}:${runId}`}
      runId={runId}
    />
  );
}

function RunDetailContent({ runId }: { runId: string }) {
  const { principal } = useAuth();
  if (!principal) throw new Error("运行详情缺少当前身份");
  const { workspace, user } = principal;
  const queryClient = useQueryClient();
  const key = runKey(workspace.id, user.id, runId);
  const [accessRevoked, setAccessRevoked] = useState(false);
  const revoked = useRef(false);
  const [streamState, setStreamState] = useState("正在连接实时事件");
  const [activeTab, setActiveTab] = useState("overview");
  const [cancelOpen, setCancelOpen] = useState(false);
  const detailNavigation = useRef<HTMLElement>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [eventError, setEventError] = useState<string | null>(null);
  const clearPrivateCache = useCallback(() => {
    const privatePrefixes = new Set([
      "runs",
      "run-context",
      "run-tools",
      "run-delivery",
      "run-repositories",
      "context-sources",
    ]);
    const related = (queryKey: readonly unknown[]) =>
      privatePrefixes.has(String(queryKey[0])) &&
      queryKey[1] === workspace.id &&
      queryKey[2] === user.id &&
      (queryKey[3] === runId ||
        (queryKey[0] === "runs" && queryKey.length === 3));
    // 停止子面板请求后移除正文缓存，避免迟到响应重新填入撤权内容。
    void queryClient.cancelQueries({
      predicate: (cached) =>
        related(cached.queryKey) && cached.queryKey[0] !== "runs",
    });
    queryClient.removeQueries({
      predicate: (cached) => related(cached.queryKey),
    });
  }, [workspace.id, user.id, runId, queryClient]);
  const forgetPrivateRun = useCallback(() => {
    revoked.current = true;
    setAccessRevoked(true);
    setEvents([]);
    clearPrivateCache();
  }, [clearPrivateCache]);
  useEffect(() => {
    // 子面板卸载后再次清理，覆盖同一时刻轮询响应与 React 提交之间的竞态。
    if (accessRevoked) clearPrivateCache();
  }, [accessRevoked, clearPrivateCache]);
  const query = useQuery({
    queryKey: key,
    queryFn: async () => {
      try {
        return await getRun(workspace.id, runId);
      } catch (error) {
        if (
          error instanceof ConsoleApiError &&
          [401, 403, 404].includes(error.status)
        )
          forgetPrivateRun();
        throw error;
      }
    },
    enabled: !accessRevoked,
    // 执行终态不是授权终态；持续展示时仍低频复验来源权限，回到窗口也立即复验。
    staleTime: 0,
    refetchOnWindowFocus: "always",
    refetchIntervalInBackground: true,
    refetchInterval: (state) =>
      state.state.data && isTerminal(state.state.data.status) ? 5000 : 2000,
    retry: (count, error) =>
      !("status" in error && [401, 403, 404].includes(Number(error.status))) &&
      count < 1,
  });
  const run = query.data;
  const cancel = useMutation({
    mutationFn: () => cancelRun(workspace.id, runId),
    onSuccess: (value) => {
      setCancelOpen(false);
      if (!revoked.current) queryClient.setQueryData(key, value);
    },
  });
  const recover = useMutation({
    mutationFn: () => recoverRun(workspace.id, runId),
    onSuccess: (value) => {
      if (!revoked.current) queryClient.setQueryData(key, value);
    },
  });

  useEffect(() => {
    if (accessRevoked) return;
    let active = true;
    let after = 0;
    let source: EventSource | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    function append(values: RunEvent[]) {
      if (!active) return;
      setEvents((current) =>
        [
          ...new Map(
            [...current, ...values].map((value) => [value.sequence, value]),
          ).values(),
        ].sort((a, b) => a.sequence - b.sequence),
      );
      after = Math.max(after, ...values.map((value) => value.sequence));
    }
    async function readPersisted() {
      try {
        let more = true;
        while (active && more) {
          const page = await getRunEvents(workspace.id, runId!, after);
          if (!active) return;
          append(page.items);
          after = Math.max(after, page.next_after);
          more = page.has_more;
          if (more && page.items.length === 0)
            throw new Error("事件分页没有前进");
        }
        setEventError(null);
      } catch (error) {
        if (
          active &&
          error instanceof ConsoleApiError &&
          [401, 403, 404].includes(error.status)
        ) {
          active = false;
          source?.close();
          if (timer) clearTimeout(timer);
          forgetPrivateRun();
          return;
        }
        if (active) setEventError(errorMessage(error));
      }
    }
    function poll() {
      if (!active) return;
      timer = setTimeout(() => void readPersisted().finally(poll), 3000);
    }
    void readPersisted().then(() => {
      if (!active) return;
      if (typeof EventSource === "undefined") {
        setStreamState("实时通道不可用，正在按序号补读");
        poll();
        return;
      }
      source = new EventSource(
        `/api/v1/workspaces/${encodeURIComponent(workspace.id)}/runs/${encodeURIComponent(runId!)}/events/stream?after=${after}`,
        { withCredentials: true },
      );
      source.onopen = () => active && setStreamState("实时事件已连接");
      const receive = (event: MessageEvent<string>) => {
        try {
          const value = JSON.parse(event.data) as RunEvent;
          // SSE 只用于触发刷新；输出、权限和最终状态仍以持久 API 为准。
          if (
            value.run_id === runId &&
            Number.isInteger(value.sequence) &&
            value.sequence > 0
          )
            append([value]);
          void queryClient.invalidateQueries({
            queryKey: runKey(workspace.id, user.id, runId),
          });
        } catch {
          setEventError("实时记录格式异常，正在回读持久记录。");
          void readPersisted();
        }
      };
      source.onmessage = receive;
      source.addEventListener("run_event", receive as EventListener);
      source.addEventListener("stream_end", () => {
        source?.close();
        if (!active) return;
        setStreamState("实时记录已同步，持续补读新增事件");
        poll();
      });
      source.onerror = () => {
        source?.close();
        if (!active) return;
        setStreamState("实时连接中断，正在按序号补读");
        poll();
      };
      source.addEventListener("access_revoked", () => {
        source?.close();
        if (!active) return;
        forgetPrivateRun();
        setEventError("访问权限已失效，请重新确认任务授权。");
      });
    });
    return () => {
      active = false;
      source?.close();
      if (timer) clearTimeout(timer);
    };
  }, [
    workspace.id,
    user.id,
    runId,
    queryClient,
    accessRevoked,
    forgetPrivateRun,
  ]);

  const backToList = (
    <Link
      to="/runs"
      className={`${buttonVariants({ variant: "outline" })} run-back-link`}
    >
      <ArrowLeft size={16} aria-hidden="true" />
      返回任务列表
    </Link>
  );
  if (accessRevoked || query.isError)
    return (
      <section className="detail-page run-detail-page">
        {backToList}
        <Banner
          closeIcon={null}
          title="无法访问此任务"
          description={
            accessRevoked
              ? "访问权限已失效，请重新确认任务授权。"
              : errorMessage(query.error)
          }
          type="danger"
        />
      </section>
    );
  if (!run) return <Spin tip="正在读取运行记录" />;
  const canCancel =
    [
      "QUEUED",
      "RUNNING",
      "WAITING_CONTEXT",
      "WAITING_APPROVAL",
      "RECOVERY_REQUIRED",
    ].includes(run.status) && !run.cancellation_requested_at;

  function showDelivery() {
    setActiveTab("delivery");
    // 手机首屏可能只看到提醒，跳转后把焦点和视口一起移到详情栏目。
    detailNavigation.current?.focus({ preventScroll: true });
    detailNavigation.current?.scrollIntoView({ block: "start" });
  }

  return (
    <section className="detail-page run-detail-page page-enter">
      {backToList}
      <header className="page-header">
        <div className="run-detail-heading">
          <Typography.Title heading={1}>{run.agent_name}</Typography.Title>
          <p>
            {run.agent_version} ·{" "}
            {run.audience === "private" ? "仅发起人可见" : "原会话受众"}
          </p>
          <small className="run-detail-id">任务 ID {run.id}</small>
        </div>
        <div className="run-actions">
          <RunStatusTag status={run.status} />
          <Button onClick={() => void query.refetch()}>刷新状态</Button>
          {["RECOVERY_REQUIRED", "WAITING_CONTEXT"].includes(run.status) ? (
            <Button
              loading={recover.isPending}
              disabled={cancel.isPending}
              onClick={() => recover.mutate()}
            >
              只读恢复
            </Button>
          ) : null}
          {canCancel ? (
            <Button
              type="danger"
              disabled={recover.isPending}
              onClick={() => {
                cancel.reset();
                setCancelOpen(true);
              }}
            >
              取消任务
            </Button>
          ) : null}
        </div>
      </header>
      {!run.execution_profile && !isTerminal(run.status) ? (
        <Banner
          closeIcon={null}
          title="执行配置尚未冻结"
          description="任务已经保存，但没有可用模型执行配置。请由管理员配置 Runtime 与 Worker 后再联调；这不代表模型已成功执行。"
          type="warning"
        />
      ) : null}
      {run.status === "WAITING_APPROVAL" && (
        <Banner
          type="warning"
          title="任务正在等待你的批准"
          description={
            <Button size="small" onClick={showDelivery}>
              查看审批计划
            </Button>
          }
        />
      )}
      {run.cancellation_requested_at && run.status !== "CANCELLED" ? (
        <Banner
          closeIcon={null}
          description="取消请求已记录，正在停止受控执行；尚未确认远端请求停止。"
          type="warning"
        />
      ) : null}
      {run.status === "RECOVERY_REQUIRED" ? (
        <Banner
          closeIcon={null}
          title="需要人工核对后恢复"
          description="执行租约或外部动作结果不确定。请核对已记录工具结果，不要盲目重复有副作用的动作。"
          type="warning"
        />
      ) : null}
      {["RECOVERY_REQUIRED", "WAITING_CONTEXT"].includes(run.status) ||
      run.recovery_read_only ? (
        <Banner
          closeIcon={null}
          title={
            run.recovery_read_only ? "只读恢复模式" : "恢复不会重复执行写操作"
          }
          description="只读恢复会重新核验来源权限并保留原有调用预算，只继续分析；本次运行的写工具与计划提案将保持禁用。未知写入或远端结果仍需人工核对。"
          type="info"
        />
      ) : null}
      {run.failure_code ? (
        <Banner
          closeIcon={null}
          title="执行未完成"
          description={`失败代码：${run.failure_code}。真实调用详情以工具事实记录为准。`}
          type="danger"
        />
      ) : null}
      {recover.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(recover.error)}
          type="danger"
        />
      ) : null}
      <section
        ref={detailNavigation}
        className="run-detail-navigation"
        aria-label="任务详情栏目"
        tabIndex={-1}
      >
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          className="run-detail-tabs"
        >
          <TabPane itemKey="overview" tab="任务概览">
            <div className="detail-columns">
              <section className="detail-card">
                <Typography.Title heading={3}>任务目标</Typography.Title>
                <pre className="run-text">{run.instruction}</pre>
              </section>
              <section className="detail-card">
                <Typography.Title heading={3}>固定执行配置</Typography.Title>
                <dl className="run-metadata">
                  <dt>模型</dt>
                  <dd>
                    {run.execution_profile
                      ? `${run.execution_profile.provider} / ${run.execution_profile.model_id}`
                      : "未配置"}
                  </dd>
                  <dt>创建</dt>
                  <dd>{formatDate(run.created_at)}</dd>
                  <dt>记录序号</dt>
                  <dd>{run.last_sequence}</dd>
                </dl>
              </section>
            </div>
            <RunReport text={run.output_text} title="Agent 回答" />
          </TabPane>
          <TabPane itemKey="context" tab="来源与资料">
            {run.project_context && (
              <RunProjectPanel
                snapshot={run.project_context}
                workspaceId={workspace.id}
                userId={user.id}
                runId={runId}
              />
            )}
            {run.audience === "group" ? (
              <RunChannelPanel
                workspaceId={workspace.id}
                userId={user.id}
                runId={runId}
              />
            ) : null}
            <RunContextPanel
              workspaceId={workspace.id}
              userId={user.id}
              runId={runId}
              cutoff={run.context_cutoff}
            />
          </TabPane>
          <TabPane itemKey="computer" tab="虚拟电脑">
            {activeTab === "computer" && (
              <div className="run-computer-layout">
                <ComputerPanel workspaceId={workspace.id} runId={runId} instruction={run.instruction} />
                <RunReport text={run.output_text} onViewArtifacts={showDelivery} />
              </div>
            )}
          </TabPane>
          <TabPane itemKey="tools" tab="工具调用">
            <ToolInvocationsPanel
              workspaceId={workspace.id}
              userId={user.id}
              runId={runId}
            />
          </TabPane>
          <TabPane itemKey="delivery" tab="审批与交付">
            <DeliveryPanel
              workspaceId={workspace.id}
              userId={user.id}
              runId={runId}
            />
          </TabPane>
          <TabPane itemKey="events" tab="事实记录">
            <section className="detail-card">
              <div className="run-section-heading">
                <Typography.Title heading={3}>运行事实记录</Typography.Title>
                <small>{streamState}</small>
              </div>
              {eventError ? (
                <Banner
                  closeIcon={null}
                  description={eventError}
                  type="warning"
                />
              ) : null}
              <ol className="run-events">
                {events.map((event) => (
                  <li key={event.sequence}>
                    <span className="run-event-number">{event.sequence}</span>
                    <strong>{event.event_type}</strong>
                    <time dateTime={event.occurred_at}>
                      {formatDate(event.occurred_at)}
                    </time>
                    <small>追踪：{event.request_id}</small>
                  </li>
                ))}
              </ol>
              {!events.length ? <p>暂无可读取的事件。</p> : null}
            </section>
          </TabPane>
        </Tabs>
      </section>
      <Modal
        title="取消任务"
        visible={cancelOpen && canCancel}
        onCancel={() => {
          if (!cancel.isPending) setCancelOpen(false);
        }}
        width={420}
        maskClosable={!cancel.isPending}
        closeOnEsc={!cancel.isPending}
      >
        <p>将请求停止此任务。提交后可在详情查看停止状态。</p>
        {cancel.isError && (
          <Banner
            closeIcon={null}
            type="danger"
            description={errorMessage(cancel.error)}
          />
        )}
        <div className="modal-actions">
          <Button
            disabled={cancel.isPending}
            onClick={() => setCancelOpen(false)}
          >
            返回任务
          </Button>
          <Button
            type="danger"
            loading={cancel.isPending}
            onClick={() => {
              if (canCancel && !cancel.isPending) cancel.mutate();
            }}
          >
            确认取消任务
          </Button>
        </div>
      </Modal>
    </section>
  );
}
