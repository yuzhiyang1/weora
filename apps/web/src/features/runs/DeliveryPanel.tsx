import {
  Banner,
  Button,
  Checkbox,
  Modal,
  Select,
  Tag,
  TextArea,
  Typography,
} from "../../components/console";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { formatDate } from "../skills/SkillsPage";
import { consoleRequest, consoleWrite, errorMessage } from "./api";

type Plan = {
  id: string;
  version: number;
  summary: string;
  risk: string;
  status:
    | "PENDING"
    | "REVISION_REQUIRED"
    | "APPROVED"
    | "REJECTED"
    | "SUPERSEDED"
    | "REVOKED";
  scope: {
    repository_id: string;
    base_revision: string;
    paths: string[];
    commands: string[][];
    actions: string[];
    remote_id: string | null;
    target_branch: string | null;
    source_delivery_id?: string | null;
    source_branch?: string | null;
    source_revision?: string | null;
  };
  decided_by: string | null;
  decided_at: string | null;
  expires_at: string | null;
  reason: string | null;
};
type Artifact = {
  id: string;
  kind: string;
  status: string;
  body: string | null;
  created_at: string;
  expires_at: string;
  sha256: string;
  command: string[];
  exit_code: number | null;
};
type Delivery = {
  id: string;
  status: string;
  business_key: string;
  attempts: number;
  remote_id: string | null;
  url: string | null;
  error_code: string | null;
  updated_at: string;
};
type Decision = "approve" | "reject" | "supplement" | "revoke";

/** 分支与提交分行显示；pending 是待核对标记，不能展示成已确认分支名。 */
function BranchDetails({ reference }: { reference: string | null }) {
  if (!reference?.startsWith("branch:")) return null;
  const [, branch, revision] = reference.split(":");
  if (!revision) return <small>分支与提交尚未确认</small>;
  return (
    <>
      <small>
        分支：<code>{branch}</code>
      </small>
      <small>
        提交：<code>{revision}</code>
      </small>
    </>
  );
}

/** 只接受 HTTPS 可点击链接；不允许供应商数据带入 javascript/file 等协议。 */
function safeRemoteUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

function screenshotUrl(artifact: Artifact): string | null {
  const body = artifact.body;
  // 只显示后台交付的有界 PNG 字节，不允许 SVG、任意 URL 或本地文件路径。
  return ["screenshot", "computer_screenshot"].includes(artifact.kind) &&
    body &&
    body.length <= 12_000_000 &&
    body.startsWith("iVBORw0KGgo") &&
    /^[A-Za-z0-9+/]+={0,2}$/.test(body)
    ? `data:image/png;base64,${body}`
    : null;
}

export function DeliveryPanel({
  workspaceId,
  userId,
  runId,
}: {
  workspaceId: string;
  userId: string;
  runId: string;
}) {
  const client = useQueryClient();
  const base = `/runs/${encodeURIComponent(runId)}`;
  const key = ["run-delivery", workspaceId, userId, runId];
  const plan = useQuery({
    queryKey: [...key, "plan"],
    queryFn: () =>
      consoleRequest<{ plan: Plan | null }>(workspaceId, `${base}/plan`),
    retry: false,
    refetchInterval: 5000,
  });
  const artifacts = useQuery({
    queryKey: [...key, "artifacts"],
    queryFn: () =>
      consoleRequest<{ items: Artifact[] }>(workspaceId, `${base}/artifacts`),
    retry: false,
    refetchInterval: 5000,
  });
  const deliveries = useQuery({
    queryKey: [...key, "deliveries"],
    queryFn: () =>
      consoleRequest<{ items: Delivery[] }>(workspaceId, `${base}/deliveries`),
    retry: false,
    refetchInterval: 5000,
  });
  const [decision, setDecision] = useState<Decision | null>(null);
  const [reviewedPlan, setReviewedPlan] = useState<Plan | null>(null);
  const reconcile = useMutation({
    mutationFn: () =>
      consoleWrite(workspaceId, `${base}/deliveries/reconcile`, {}),
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: [...key, "deliveries"] }),
  });
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [expires, setExpires] = useState(3600);
  const current = plan.isError ? null : plan.data?.plan;
  const mutation = useMutation({
    mutationFn: (value: Decision) =>
      consoleWrite(workspaceId, `${base}/plan/${reviewedPlan!.id}/decision`, {
        decision: value,
        expires_in_seconds: expires,
        ...(reason.trim() ? { reason: reason.trim() } : {}),
      }),
    onSuccess: () => {
      setDecision(null);
      setReviewedPlan(null);
      setReason("");
      setConfirmed(false);
      void client.invalidateQueries({ queryKey: key });
      void client.invalidateQueries({ queryKey: ["inbox", workspaceId, userId] });
      void client.invalidateQueries({
        queryKey: ["runs", workspaceId, userId, runId],
      });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (decision && current && reviewedPlan?.id === current.id)
      mutation.mutate(decision);
  }
  function openDecision(value: Decision) {
    // 审批确认绑定审阅时的不可变版本，轮询发现新计划不能沿用旧复选确认。
    setReviewedPlan(current ?? null);
    setDecision(value);
    setConfirmed(false);
    setReason("");
  }
  const reviewChanged =
    !!decision &&
    (reviewedPlan?.id !== current?.id ||
      reviewedPlan?.status !== current?.status);
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null);
  // 正文只来自当前成功的鉴权请求；错误或过期时不继续展示旧缓存正文。
  const visibleArtifact = artifacts.isError
    ? undefined
    : artifacts.data?.items.find((item) => item.id === selectedArtifact);

  return (
    <>
      <section className="detail-card">
        <Typography.Title heading={3}>执行计划与人工授权</Typography.Title>
        {plan.isError ? (
          <Banner
            closeIcon={null}
            description={errorMessage(plan.error)}
            type="warning"
          />
        ) : null}
        {!current && !plan.isError ? (
          <p>
            暂无待审批计划。Agent
            提案不能代替人类授权；普通修复请求不隐含写仓库、推送或创建 PR 权限。
          </p>
        ) : null}
        {current ? (
          <>
            <div className="run-actions">
              <Tag>计划 v{current.version}</Tag>
              <Tag color={current.status === "APPROVED" ? "green" : "grey"}>
                {planLabel(current.status)}
              </Tag>
            </div>
            <p className="run-text">{current.summary}</p>
            <Banner
              closeIcon={null}
              title="风险与边界"
              description={current.risk}
              type="warning"
            />
            <dl className="run-metadata run-metadata--approval">
              <dt>目标仓库</dt>
              <dd>{current.scope.repository_id}</dd>
              <dt>固定基准</dt>
              <dd>
                <code>{current.scope.base_revision}</code>
              </dd>
              <dt>修改范围</dt>
              <dd>{current.scope.paths.join("、")}</dd>
              <dt>批准动作</dt>
              <dd>
                {current.scope.actions.map((action) => (
                  <Tag key={action}>{action}</Tag>
                ))}
              </dd>
              <dt>登记远端 / 基准与 MR 目标分支</dt>
              <dd>
                {current.scope.remote_id
                  ? `${current.scope.remote_id} / ${current.scope.target_branch ?? "未指定"}`
                  : "未授权远端操作"}
              </dd>
              {current.scope.actions.includes("git.push") && (
                <>
                  <dt>实际推送分支</dt>
                  <dd>
                    {current.scope.source_branch
                      ? `续写 ${current.scope.source_branch}，远端必须仍为 ${current.scope.source_revision}；不写入目标分支。`
                      : "平台生成的 weora/ 专用新分支，不写入上述目标分支。"}
                  </dd>
                </>
              )}
              {current.scope.source_delivery_id ? (
                <>
                  <dt>已交付分支记录</dt>
                  <dd>
                    {current.scope.source_delivery_id} ·{" "}
                    {current.scope.actions.includes("git.push")
                      ? "按新审批续写原分支，不重复创建 PR，不合并"
                      : "仅创建普通 MR，不重新推送、不合并"}
                  </dd>
                </>
              ) : null}
            </dl>
            <Typography.Title heading={5}>允许的精确命令</Typography.Title>
            {current.scope.commands.length ? (
              current.scope.commands.map((command, index) => (
                <pre className="run-text" key={index}>
                  {JSON.stringify(command)}
                </pre>
              ))
            ) : (
              <p>没有命令执行授权。</p>
            )}
            {current.expires_at ? (
              <p>授权有效期至 {formatDate(current.expires_at)}</p>
            ) : null}
            {current.decided_by ? (
              <p>
                决策成员：{current.decided_by} ·{" "}
                {current.decided_at ? formatDate(current.decided_at) : ""}
              </p>
            ) : null}
            {current.reason ? <p>决策说明：{current.reason}</p> : null}
            {current.status === "REVISION_REQUIRED" ? (
              <p>
                已要求 Agent
                重拟。当前范围不可批准；新提案提交后将再次等待审批。
              </p>
            ) : null}
            <div className="run-actions">
              {current.status === "PENDING" ? (
                <>
                  <Button
                    theme="solid"
                    onClick={() => {
                      openDecision("approve");
                    }}
                  >
                    审阅并批准
                  </Button>
                  <Button type="danger" onClick={() => openDecision("reject")}>
                    拒绝当前计划
                  </Button>
                  <Button onClick={() => openDecision("supplement")}>
                    补充上下文 / 要求重拟
                  </Button>
                </>
              ) : null}
              {current.status === "APPROVED" ? (
                <Button type="danger" onClick={() => openDecision("revoke")}>
                  撤销执行授权
                </Button>
              ) : null}
            </div>
          </>
        ) : null}
        <Modal
          centered
          title={decision === "approve" ? "确认具体执行范围" : "记录人工决策"}
          visible={!!decision && !plan.isError}
          footer={null}
          maskClosable={!mutation.isPending}
          closeOnEsc={!mutation.isPending}
          onCancel={() => {
            if (!mutation.isPending) setDecision(null);
          }}
          width={640}
        >
          <form className="agent-form" onSubmit={submit}>
            {reviewChanged ? (
              <Banner
                closeIcon={null}
                title="计划已更新或不可访问"
                description="本次确认已失效，请返回重新核对当前计划版本。"
                type="warning"
              />
            ) : null}
            {decision === "approve" ? (
              <>
                <Banner
                  closeIcon={null}
                  title="本次授权只覆盖当前计划版本"
                  description={`计划 v${reviewedPlan?.version ?? ""}；仓库 ${reviewedPlan?.scope.repository_id ?? ""}；动作 ${reviewedPlan?.scope.actions.join("、") ?? ""}。任何关键范围变更都需要重新审批，不包括合并与生产部署。`}
                  type="warning"
                />
                <div className="form-field">
                  <span id="plan-expiry-label">授权有效期</span>
                  <Select
                    aria-labelledby="plan-expiry-label"
                    value={expires}
                    optionList={[
                      { value: 900, label: "15 分钟" },
                      { value: 3600, label: "1 小时" },
                    ]}
                    onChange={(value) => setExpires(Number(value))}
                  />
                </div>
                <Checkbox
                  checked={confirmed}
                  onChange={(event) =>
                    setConfirmed(event.target.checked ?? false)
                  }
                >
                  我已核对仓库、路径、命令、风险及远端动作
                </Checkbox>
              </>
            ) : null}
            <label className="form-field">
              <span>
                {decision === "supplement" ? "补充的上下文与约束" : "决策说明"}
              </span>
              <TextArea
                rows={4}
                maxLength={4000}
                value={reason}
                onChange={setReason}
                required={decision === "supplement"}
              />
            </label>
            {mutation.isError ? (
              <Banner
                closeIcon={null}
                description={errorMessage(mutation.error)}
                type="danger"
              />
            ) : null}
            <div className="modal-actions">
              <Button
                disabled={mutation.isPending}
                onClick={() => setDecision(null)}
              >
                返回审阅
              </Button>
              <Button
                htmlType="submit"
                theme="solid"
                loading={mutation.isPending}
                disabled={
                  reviewChanged ||
                  (decision === "approve" && !confirmed) ||
                  (decision === "supplement" && !reason.trim())
                }
              >
                确认记录决策
              </Button>
            </div>
          </form>
        </Modal>
      </section>
      <section className="detail-card">
        <Typography.Title heading={3}>工具产物与验证证据</Typography.Title>
        <p>
          以下状态来自执行工具，不以 Agent 自述代替测试、浏览器截图或 Diff。
        </p>
        {artifacts.isError ? (
          <Banner
            closeIcon={null}
            description={errorMessage(artifacts.error)}
            type="warning"
          />
        ) : null}
        <ul className="context-records">
          {!artifacts.isError
            ? artifacts.data?.items.map((item) => (
                <li key={item.id}>
                  <span>
                    <strong>{artifactKind(item.kind)}</strong> ·{" "}
                    {artifactStatus(item.status)}
                    <small>
                      {item.command.length
                        ? `真实 argv：${JSON.stringify(item.command)} · `
                        : ""}
                      {item.exit_code === null
                        ? "未记录退出码"
                        : `退出码：${item.exit_code}`}{" "}
                      · {formatDate(item.created_at)}
                    </small>
                    <small>SHA-256：{item.sha256}</small>
                  </span>
                  <Button
                    disabled={item.body === null}
                    onClick={() => {
                      setSelectedArtifact(item.id);
                      void artifacts.refetch();
                    }}
                  >
                    {item.body === null ? "正文已过期 / 不可访问" : "查看证据"}
                  </Button>
                </li>
              ))
            : null}
        </ul>
        {!artifacts.isError && artifacts.data?.items.length === 0 ? (
          <p>尚无产物；不表示测试通过或浏览器已验证。</p>
        ) : null}
        {visibleArtifact ? (
          <section>
            <Typography.Title heading={5}>
              {artifactKind(visibleArtifact.kind)}
            </Typography.Title>
            {/* 预览明确绑定产物 ID 和真实状态，便于与 Agent 引用及失败断言核对。 */}
            <p className="run-text">
              证据 ID：{visibleArtifact.id} ·{" "}
              {artifactStatus(visibleArtifact.status)}
            </p>
            {visibleArtifact.body === null ? (
              <Banner
                closeIcon={null}
                description="证据正文已过期或授权已失效，不能继续预览。"
                type="warning"
              />
            ) : screenshotUrl(visibleArtifact) ? (
              <img
                className="run-screenshot"
                alt="受控浏览器验收截图"
                src={screenshotUrl(visibleArtifact)!}
              />
            ) : (
              <pre className="run-text">{visibleArtifact.body}</pre>
            )}
          </section>
        ) : null}
      </section>
      <section className="detail-card">
        <Typography.Title heading={3}>代码托管交付</Typography.Title>
        <p>
          分支推送与 MR/PR
          分别记录：推送成功不代表已创建评审。飞书消息回执见“飞书入口与原会话回报”。未知结果不能盲目重发。
        </p>
        {deliveries.isError ? (
          <Banner
            closeIcon={null}
            description={errorMessage(deliveries.error)}
            type="warning"
          />
        ) : null}
        <ul className="context-records">
          {!deliveries.isError
            ? deliveries.data?.items.map((item) => (
                <li key={item.id}>
                  <span>
                    <strong>
                      {item.remote_id?.startsWith("branch:")
                        ? "测试分支推送（不是 MR/PR）"
                        : item.remote_id?.startsWith("review:")
                          ? "普通 MR（非草稿，不自动合并）"
                          : "MR/PR 交付"}
                    </strong>
                    <Tag>{deliveryLabel(item.status)}</Tag>
                    <BranchDetails reference={item.remote_id} />
                    <small>
                      尝试 {item.attempts} 次 · {formatDate(item.updated_at)}
                    </small>
                    {item.error_code ? (
                      <small>失败代码：{item.error_code}</small>
                    ) : null}
                    {item.status === "NOT_SENT" ? (
                      <small>
                        预检失败，未发送创建请求；修复后仍需新计划人工审批。
                      </small>
                    ) : null}
                  </span>
                  {safeRemoteUrl(item.url) ? (
                    <a
                      href={safeRemoteUrl(item.url)!}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      {item.remote_id?.startsWith("branch:")
                        ? "打开仓库"
                        : "查看已确认远端结果"}
                    </a>
                  ) : null}
                  {["UNKNOWN", "RECOVERY_REQUIRED"].includes(item.status) &&
                  item.remote_id?.startsWith("branch:") ? (
                    <small>
                      分支结果未知，需人工核对远端；不会自动重推，也不使用 MR
                      核对结果代替。
                    </small>
                  ) : ["UNKNOWN", "RECOVERY_REQUIRED"].includes(item.status) ? (
                    <Button
                      loading={reconcile.isPending}
                      onClick={() => reconcile.mutate()}
                    >
                      核对远端（只读）
                    </Button>
                  ) : null}
                </li>
              ))
            : null}
        </ul>
        {reconcile.isError ? (
          <Banner
            closeIcon={null}
            description={errorMessage(reconcile.error)}
            type="warning"
          />
        ) : null}
        {!deliveries.isError && deliveries.data?.items.length === 0 ? (
          <p>尚无已授权的代码托管交付记录。</p>
        ) : null}
      </section>
    </>
  );
}

function planLabel(value: string) {
  return (
    (
      {
        PENDING: "待审批",
        REVISION_REQUIRED: "等待重拟",
        APPROVED: "已批准",
        REJECTED: "已拒绝",
        SUPERSEDED: "旧版本已失效",
        REVOKED: "已撤销",
      } as Record<string, string>
    )[value] ?? value
  );
}
function artifactKind(value: string) {
  return (
    (
      {
        diff: "代码 Diff",
        test: "测试报告",
        command: "命令记录",
        browser: "浏览器验证",
        screenshot: "浏览器截图",
        computer_screenshot: "虚拟电脑网页截图",
        report: "执行报告",
        "review.recovery": "MR 预检人工核对",
      } as Record<string, string>
    )[value] ?? value
  );
}
function artifactStatus(value: string) {
  return (
    (
      {
        SUCCEEDED: "验证通过",
        NOT_SENT: "预检未发送（不代表创建成功）",
        PASSED: "验证通过",
        FAILED: "验证失败",
        NOT_CONFIGURED: "未配置",
        NOT_APPLICABLE: "不适用",
        SERVICE_FAILED: "服务启动失败",
        SERVICE_NOT_READY: "服务未就绪",
        URL_DENIED: "越界访问已拒绝",
        SCREENSHOT_FAILED: "截图失败",
        TIMEOUT: "验证超时",
        ASSERTION_FAILED: "断言失败",
        SKIPPED: "未执行",
        EXPIRED: "正文已过期",
        passed: "验证通过",
        failed: "验证失败",
        not_configured: "未配置",
        not_applicable: "不适用",
      } as Record<string, string>
    )[value] ?? value
  );
}
function deliveryLabel(value: string) {
  return (
    (
      {
        PENDING: "待投递",
        SENDING: "正在交付",
        NOT_SENT: "预检失败，未发送",
        SUCCEEDED: "已确认送达",
        DELIVERED: "已确认送达",
        FAILED: "投递失败",
        UNKNOWN: "结果未知，需核对",
        RECOVERY_REQUIRED: "需要人工恢复",
      } as Record<string, string>
    )[value] ?? value
  );
}
