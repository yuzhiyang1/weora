import { useDiscardChanges } from "../settings/useDiscardChanges";
import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Banner,
  Button,
  Input,
  Modal,
  Select,
  Spin,
  TextArea,
} from "../../components/console";
import {
  getSetupOptions,
  saveCoordination,
  coordinationError,
  type CoordinationConfig,
  type SetupOptions,
} from "./api";

/** 从已授权路由、仓库和发布资产创建范围；默认观察，不能把配置提交当作实施或工具批准。 */
export function SetupDialog({
  workspaceId,
  close,
}: {
  workspaceId: string;
  close: () => void;
}) {
  const options = useQuery({
    queryKey: ["coordination-setup", workspaceId],
    queryFn: () => getSetupOptions(workspaceId),
    retry: false,
  });
  if (options.data)
    return (
      <SetupForm
        workspaceId={workspaceId}
        options={options.data}
        close={close}
      />
    );
  return (
    <Modal title="新增协调范围" visible onCancel={close} footer={null}>
      {options.isPending ? (
        <Spin tip="正在读取已配置的来源与发布版本" />
      ) : (
        <Banner
          type="danger"
          title="无法读取配置选项"
          description={coordinationError(options.error)}
        />
      )}
    </Modal>
  );
}

function SetupForm({
  workspaceId,
  options,
  close,
}: {
  workspaceId: string;
  options: SetupOptions;
  close: () => void;
}) {
  const client = useQueryClient();
  const [kind, setKind] = useState<"feishu" | "code_hosting">("feishu");
  const [sourceKey, setSourceKey] = useState("");
  const [mainKey, setMainKey] = useState("");
  const [workerKey, setWorkerKey] = useState("");
  const [actors, setActors] = useState<string[]>([]);
  const [login, setLogin] = useState("");
  const [externalActor, setExternalActor] = useState("");
  const [instruction, setInstruction] = useState(
    "仅在请求明确要求实施、目标和验收条件清楚且符合项目既有规范时接单；普通讨论不接单。",
  );
  const draft = JSON.stringify({
    kind,
    sourceKey,
    mainKey,
    workerKey,
    actors,
    login,
    externalActor,
    instruction,
  });
  const [baseline] = useState(draft);
  const { requestClose, confirmation } = useDiscardChanges(
    draft !== baseline,
    close,
  );
  const route = options.routes.find((item) => item.route_key === sourceKey);
  const repository = options.repositories.find(
    (item) =>
      `${item.project_id ?? "-"}:${item.repository_id}:${item.remote_id}` ===
      sourceKey,
  );
  const main = options.agents.find(
    (item) => `${item.agent_id}:${item.version}` === mainKey,
  );
  const worker =
    kind === "feishu"
      ? route && {
          agent_id: route.worker_agent_id,
          version: route.worker_agent_version,
        }
      : options.agents.find(
          (item) => `${item.agent_id}:${item.version}` === workerKey,
        );
  const available =
    kind === "feishu"
      ? options.routes.map((item) => ({
          value: item.route_key,
          label: item.label,
        }))
      : options.repositories.map((item) => ({
          value: `${item.project_id ?? "-"}:${item.repository_id}:${item.remote_id}`,
          label: item.label,
        }));
  const ready =
    main &&
    worker &&
    (kind === "feishu"
      ? route && actors.length > 0
      : repository && login.trim() && externalActor.trim());
  const save = useMutation({
    mutationFn: async () => {
      if (!ready || !main || !worker)
        throw new Error("请选择来源、固定版本和接单身份");
      const config: CoordinationConfig = {
        id: "",
        source_id: route?.source_id ?? "",
        route_key: route?.route_key ?? "",
        source_kind: kind,
        main_agent_id: main.agent_id,
        main_agent_version: main.version,
        worker_agent_id: worker.agent_id,
        worker_agent_version: worker.version,
        mode: "observe",
        enabled: true,
        paused: false,
        allowed_actors: kind === "feishu" ? actors : [externalActor.trim()],
        rules: [
          {
            id: "implementation",
            name: "实施请求",
            instruction,
            enabled: true,
          },
        ],
        revision: 0,
        max_workers: 3,
        main_model_id: null,
        worker_model_id: null,
        project_id: repository?.project_id ?? null,
        repository_source: repository
          ? {
              repository_id: repository.repository_id,
              remote_id: repository.remote_id,
              agent_login: login.trim(),
              actors: { [externalActor.trim()]: options.user_id },
              observe_issues: repository.provider === "github",
              observe_pulls: true,
              observe_comments: true,
              observe_ci: false,
            }
          : null,
      };
      return saveCoordination(workspaceId, config);
    },
    onSuccess: async () => {
      await client.invalidateQueries({
        queryKey: ["coordination", workspaceId],
      });
      close();
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (ready && !save.isPending) save.mutate();
  }
  return (
    <>
      <Modal
        title="新增协调范围"
        visible
        onCancel={() => {
          if (!save.isPending) requestClose();
        }}
        maskClosable={!save.isPending}
        closeOnEsc={!save.isPending}
        footer={null}
      >
        <form className="coordination-config-form" onSubmit={submit}>
          <fieldset disabled={save.isPending} className="coordination-fields">
            <label>
              消息来源
              <Select
                aria-label="消息来源"
                value={kind}
                optionList={[
                  { value: "feishu", label: "飞书群 / 话题" },
                  { value: "code_hosting", label: "代码仓库讨论" },
                ]}
                onChange={(value) => {
                  setKind(value as typeof kind);
                  setSourceKey("");
                  setActors([]);
                }}
              />
            </label>
            <label>
              已配置入口
              <Select
                aria-label="已配置入口"
                placeholder="选择已授权的来源"
                value={sourceKey}
                optionList={available}
                onChange={(value) => {
                  setSourceKey(String(value));
                  setActors([]);
                }}
              />
            </label>
            {available.length === 0 && (
              <p>
                当前没有可用入口。
                <Link to={kind === "feishu" ? "/context" : "/settings/code"}>
                  先登记连接、来源和权限
                </Link>
                。
              </p>
            )}
            <label>
              主 Agent 固定版本
              <Select
                aria-label="主 Agent 固定版本"
                placeholder="选择发布版本"
                value={mainKey}
                optionList={options.agents.map((item) => ({
                  value: `${item.agent_id}:${item.version}`,
                  label: item.label,
                }))}
                onChange={(value) => setMainKey(String(value))}
              />
            </label>
            {kind === "feishu" ? (
              <>
                <p>
                  Worker 沿用所选执行入口绑定的版本{" "}
                  {route?.worker_agent_version ?? "—"}。
                </p>
                <label>
                  允许接单的飞书账号
                  <Select
                    aria-label="允许接单的飞书账号"
                    multiple
                    value={actors}
                    placeholder="选择已绑定且获来源授权的账号"
                    optionList={(route?.actors ?? []).map((value) => ({
                      value,
                      label: value,
                    }))}
                    onChange={(value) => setActors(value as string[])}
                  />
                </label>
              </>
            ) : (
              <>
                <label>
                  Worker 固定版本
                  <Select
                    aria-label="Worker 固定版本"
                    placeholder="选择发布版本"
                    value={workerKey}
                    optionList={options.agents.map((item) => ({
                      value: `${item.agent_id}:${item.version}`,
                      label: item.label,
                    }))}
                    onChange={(value) => setWorkerKey(String(value))}
                  />
                </label>
                <label>
                  机器人账号名
                  <Input
                    value={login}
                    required
                    maxLength={256}
                    onChange={setLogin}
                    placeholder="用于识别 @ 提及"
                  />
                </label>
                <label>
                  我的外部账号
                  <Input
                    value={externalActor}
                    required
                    maxLength={256}
                    onChange={setExternalActor}
                    placeholder={
                      repository?.provider === "codeup"
                        ? "Codeup 用户 ID"
                        : "GitHub 登录名"
                    }
                  />
                </label>
                <p>
                  此账号的实施请求绑定到当前 Weora
                  用户。其他账号不会借用你的身份接单；代码写入仍需逐 Run 审批。
                </p>
              </>
            )}
            <label>
              自动接单规则
              <TextArea
                value={instruction}
                onChange={setInstruction}
                required
                maxLength={4000}
              />
            </label>
            <p>
              先以观察模式保存：记录 Main
              判断并验证规则，确认后可在范围配置中开启自动接单。
            </p>
          </fieldset>
          {save.isError && (
            <Banner
              type="danger"
              title="范围未创建"
              description={coordinationError(save.error)}
            />
          )}
          <div className="coordination-actions">
            <Button disabled={save.isPending} onClick={requestClose}>
              取消
            </Button>
            <Button
              type="primary"
              htmlType="submit"
              disabled={!ready}
              loading={save.isPending}
            >
              保存并开始观察
            </Button>
          </div>
        </form>
      </Modal>
      {confirmation}
    </>
  );
}
