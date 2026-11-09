import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Banner,
  Button,
  Card,
  Input,
  Modal,
  Select,
  Spin,
  Table,
  Tag,
  TextArea,
  Typography,
} from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import {
  listCoordination,
  saveCoordination,
  coordinationError,
  getCoordinationRecords,
  type CoordinationConfig,
  type CoordinationResource,
  type CoordinationScope,
  type CoordinationTicket,
} from "./api";
import "./coordination.css";
import { SetupDialog } from "./SetupDialog";

const modes = { off: "仅显式请求", observe: "观察并记录", auto: "按规则执行" };
const states: Record<string, string> = {
  running: "执行中",
  pending: "待处理",
  queued: "排队中",
  completed: "已完成",
  ready: "目标完成",
  waiting: "等待新输入",
  blocked: "等待人类",
  failed: "失败",
  dropped: "已放弃",
  unknown: "结果未知",
  done: "已处理",
  cancelled: "已取消",
  sent: "已投递",
  stopped: "已停止",
  progress: "进行中",
};
const resources: { value: CoordinationResource; label: string }[] = [
  { value: "threads", label: "线程与订阅" },
  { value: "decisions", label: "接单与路由判断" },
  { value: "activities", label: "后台活动" },
  { value: "effects", label: "派发与重试" },
  { value: "sessions", label: "执行会话" },
  { value: "notices", label: "协调通知" },
];

export function CoordinationPage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("协调中心缺少当前身份");
  return (
    <CoordinationWorkspace
      key={`${principal.workspace.id}:${principal.user.id}`}
      workspaceId={principal.workspace.id}
      manager={["owner", "admin"].includes(principal.workspace.role)}
    />
  );
}

export function CoordinationWorkspace({
  workspaceId,
  manager,
}: {
  workspaceId: string;
  manager: boolean;
}) {
  const [setup, setSetup] = useState(false);
  const scopes = useQuery({
    queryKey: ["coordination", workspaceId],
    queryFn: () => listCoordination(workspaceId),
    enabled: manager,
    retry: false,
    refetchInterval: 5000,
  });
  return (
    <section className="asset-page coordination-page page-enter">
      <header className="page-header">
        <div>
          <span className="page-eyebrow">AGENT COORDINATION</span>
          <Typography.Title heading={1}>协调中心</Typography.Title>
          <Typography.Paragraph>
            查看主 Agent 的判断、Worker 进度和需要人类处理的事项。
          </Typography.Paragraph>
        </div>
        <div className="coordination-actions">
          <Button
            onClick={() => void scopes.refetch()}
            disabled={!manager || scopes.isFetching}
          >
            刷新状态
          </Button>
          {manager && (
            <Button type="primary" onClick={() => setSetup(true)}>
              新增协调范围
            </Button>
          )}
        </div>
      </header>
      {!manager ? (
        <Banner
          type="warning"
          title="需要工作空间管理员权限"
          description="任务与审批仍可从运行页面查看。"
        />
      ) : scopes.isPending ? (
        <Spin tip="正在读取协调状态" />
      ) : scopes.isError ? (
        <Banner
          type="danger"
          title="协调状态读取失败"
          description={coordinationError(scopes.error)}
        />
      ) : scopes.data.items.length === 0 ? (
        <Card title="尚未配置主 Agent">
          <p>先在飞书上下文登记来源和执行入口，再绑定主 Agent 的固定版本。</p>
          <Link to="/context">管理飞书上下文</Link>
        </Card>
      ) : (
        scopes.data.items.map((scope) => (
          <ScopePanel
            key={scope.config.id}
            workspaceId={workspaceId}
            scope={scope}
          />
        ))
      )}
      {setup && (
        <SetupDialog workspaceId={workspaceId} close={() => setSetup(false)} />
      )}
    </section>
  );
}

function ScopePanel({
  workspaceId,
  scope,
}: {
  workspaceId: string;
  scope: CoordinationScope;
}) {
  const [editing, setEditing] = useState(false);
  const { config, status } = scope;
  const tickets = status.tickets ?? [];
  return (
    <Card
      title={
        <>
          主 Agent{" "}
          <Tag color={config.paused || !config.enabled ? "orange" : "blue"}>
            {!config.enabled
              ? "已停用"
              : config.paused
                ? "已暂停"
                : modes[config.mode]}
          </Tag>
        </>
      }
      headerExtraContent={
        <Button onClick={() => setEditing(true)}>调整协调配置</Button>
      }
    >
      <div className="coordination-summary">
        <p>
          {config.source_kind === "code_hosting" ? "代码仓库讨论" : "飞书消息"}{" "}
          · 主会话版本 {config.main_agent_version} · Worker 版本{" "}
          {config.worker_agent_version}
        </p>
        <p>
          <Link to={`/agents/${config.main_agent_id}`}>
            查看主 Agent 版本与提示词
          </Link>{" "}
          ·{" "}
          <Link to={`/agents/${config.worker_agent_id}`}>
            查看 Worker 版本与提示词
          </Link>
        </p>
        <p>
          自动并发上限 {config.max_workers} · 当前待判断{" "}
          {status.evaluations?.length ?? 0}
        </p>
      </div>
      {tickets.some((item) => item.attention) && (
        <Banner
          type="warning"
          title="有任务等待人类处理"
          description="进入对应运行查看问题或审批，状态询问不会重新执行任务。"
        />
      )}
      <Table<CoordinationTicket>
        rowKey="id"
        dataSource={tickets}
        columns={[
          { title: "任务", dataIndex: "title" },
          {
            title: "状态",
            render: (_, item) =>
              !item.outcome && item.assignment?.report?.state === "ready"
                ? "等待评审"
                : (states[
                    item.outcome ?? item.assignment?.report?.state ?? "queued"
                  ] ?? "等待处理"),
          },
          {
            title: "最新报告",
            render: (_, item) =>
              item.attention ??
              item.assignment?.report?.summary ??
              "等待 Worker 报告",
          },
          {
            title: "运行",
            render: (_, item) =>
              item.assignment?.run_id ? (
                <Link to={`/runs/${item.assignment.run_id}`}>查看运行</Link>
              ) : (
                "尚未派发"
              ),
          },
        ]}
      />
      <div className="coordination-status-grid">
        <div>
          <h3>会话与模型</h3>
          {(status.sessions ?? []).map((item) => (
            <p key={item.id}>
              {item.role === "main"
                ? "主会话"
                : item.role === "subagent"
                  ? "子会话"
                  : "Worker"}
              {" · "}
              {item.model_id ?? "使用部署模型"}
              {item.model_retry &&
                ` · 第 ${item.model_retry.attempt} 次重试，退避 ${item.model_retry.delay_ms / 1000} 秒`}
            </p>
          ))}
          {!status.sessions?.length && <p>尚未启动会话</p>}
        </div>
        <div>
          <h3>投递与异常</h3>
          <p>
            待发回复{" "}
            {scope.deliveries.filter((item) => item.state === "pending").length}{" "}
            · 未知回复{" "}
            {scope.deliveries.filter((item) => item.state === "unknown").length}
          </p>
          {scope.effects
            .filter((item) => item.error)
            .map((item, index) => (
              <p key={index}>
                {item.kind} · {item.error} · 尝试 {item.attempts} 次
              </p>
            ))}
        </div>
      </div>
      <ScopeRecords workspaceId={workspaceId} scopeId={config.id} />
      {editing && (
        <ConfigEditor
          key={config.revision}
          workspaceId={workspaceId}
          initial={config}
          close={() => setEditing(false)}
        />
      )}
    </Card>
  );
}

function ScopeRecords({
  workspaceId,
  scopeId,
}: {
  workspaceId: string;
  scopeId: string;
}) {
  const [resource, setResource] = useState<CoordinationResource>("threads");
  const [offset, setOffset] = useState(0);
  const [opened, setOpened] = useState(false);
  const records = useQuery({
    queryKey: ["coordination-records", workspaceId, scopeId, resource, offset],
    queryFn: () =>
      getCoordinationRecords(workspaceId, scopeId, resource, offset),
    enabled: opened,
    retry: false,
    refetchInterval: opened ? 5000 : false,
  });
  const items =
    records.data?.items ??
    records.data?.sessions ??
    records.data?.notices ??
    [];
  return (
    <details onToggle={(event) => setOpened(event.currentTarget.open)}>
      <summary>查看线程、活动与判断明细</summary>
      <div className="coordination-record-toolbar">
        <Select
          aria-label="明细类型"
          value={resource}
          optionList={resources}
          onChange={(value) => {
            setResource(value as CoordinationResource);
            setOffset(0);
          }}
        />
        <Button
          disabled={offset === 0 || records.isFetching}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          上一页
        </Button>
        <Button
          disabled={records.data?.next_offset == null || records.isFetching}
          onClick={() => setOffset(records.data?.next_offset ?? offset)}
        >
          下一页
        </Button>
      </div>
      {records.isError ? (
        <Banner
          type="danger"
          title="明细读取失败"
          description={coordinationError(records.error)}
        />
      ) : (
        <Table
          rowKey="id"
          dataSource={items}
          loading={records.isPending}
          columns={[
            { title: "记录", dataIndex: "id" },
            {
              title: "状态 / 类型",
              render: (_, item) =>
                states[item.state ?? ""] ?? item.kind ?? "已记录",
            },
            {
              title: "说明",
              render: (_, item) =>
                item.reason ??
                item.summary ??
                item.result?.text ??
                item.result?.code ??
                item.model_id ??
                (item.subscriptions
                  ? `关联 ${item.subscriptions.length} 个任务，待判断 ${item.pending?.length ?? 0} 条`
                  : "可在对应任务查看详情"),
            },
          ]}
        />
      )}
    </details>
  );
}

function ConfigEditor({
  workspaceId,
  initial,
  close,
}: {
  workspaceId: string;
  initial: CoordinationConfig;
  close: () => void;
}) {
  const [config, setConfig] = useState(initial);
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: () => saveCoordination(workspaceId, config),
    onSuccess: async () => {
      await client.invalidateQueries({
        queryKey: ["coordination", workspaceId],
      });
      close();
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!save.isPending) save.mutate();
  }
  return (
    <Modal
      title="调整协调配置"
      visible
      onCancel={() => {
        if (!save.isPending) close();
      }}
      maskClosable={!save.isPending}
      closeOnEsc={!save.isPending}
      footer={null}
    >
      <form className="coordination-config-form" onSubmit={submit}>
        <label>
          接单模式
          <Select
            aria-label="接单模式"
            value={config.mode}
            optionList={Object.entries(modes).map(([value, label]) => ({
              value,
              label,
            }))}
            onChange={(value) =>
              setConfig({
                ...config,
                mode: value as CoordinationConfig["mode"],
              })
            }
          />
        </label>
        <label>
          运行状态
          <Select
            aria-label="运行状态"
            value={config.paused ? "paused" : "active"}
            optionList={[
              { value: "active", label: "正常调度" },
              { value: "paused", label: "暂停调度" },
            ]}
            onChange={(value) =>
              setConfig({ ...config, paused: value === "paused" })
            }
          />
        </label>
        <label>
          自动并发上限
          <Input
            type="number"
            min={1}
            max={10}
            required
            value={String(config.max_workers)}
            onChange={(value) =>
              setConfig({ ...config, max_workers: Number(value) })
            }
          />
        </label>
        <label>
          主会话模型
          <Input
            value={config.main_model_id ?? ""}
            placeholder="留空使用部署模型"
            onChange={(value) =>
              setConfig({ ...config, main_model_id: value.trim() || null })
            }
          />
        </label>
        <label>
          Worker 模型
          <Input
            value={config.worker_model_id ?? ""}
            placeholder="留空使用部署模型"
            onChange={(value) =>
              setConfig({ ...config, worker_model_id: value.trim() || null })
            }
          />
        </label>
        <p>
          模型调整仅影响新会话和新接单；现有任务的版本、执行批准及来源授权保持独立。
        </p>
        {config.rules.map((rule, index) => (
          <label key={rule.id}>
            {rule.name}
            <TextArea
              value={rule.instruction}
              required
              maxLength={4000}
              onChange={(instruction) =>
                setConfig({
                  ...config,
                  rules: config.rules.map((item, at) =>
                    at === index ? { ...item, instruction } : item,
                  ),
                })
              }
            />
          </label>
        ))}
        {save.isError && (
          <Banner
            type="danger"
            title="配置未保存"
            description={coordinationError(save.error)}
          />
        )}
        <div className="coordination-actions">
          <Button disabled={save.isPending} onClick={close}>
            取消
          </Button>
          <Button htmlType="submit" type="primary" loading={save.isPending}>
            保存配置
          </Button>
        </div>
      </form>
    </Modal>
  );
}
