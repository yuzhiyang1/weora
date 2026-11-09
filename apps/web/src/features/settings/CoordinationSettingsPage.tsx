import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  Button,
  Card,
  Empty,
  Input,
  Select,
  TextArea,
  Checkbox,
} from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import {
  listCoordination,
  saveCoordination,
  type CoordinationConfig,
} from "../coordination/api";
import { SetupDialog } from "../coordination/SetupDialog";
import { CoordinationModelField } from "./CoordinationModelField";
import {
  useSettingsDirty,
  useSettingsEnvironment,
} from "./SettingsEnvironment";
import {
  SettingsLoading,
  SettingsPage,
  SettingsResult,
  useTeamManager,
} from "./SettingsPage";

export function CoordinationSettingsPage() {
  const { principal } = useAuth();
  const manager = useTeamManager();
  const env = useSettingsEnvironment();
  const workspace = principal!.workspace.id;
  const query = useQuery({
    queryKey: ["coordination", workspace],
    queryFn: () => listCoordination(workspace),
    enabled: manager,
  });
  const [selected, setSelected] = useState("");
  const [setup, setSetup] = useState(false);
  const scopes = query.isError ? [] : (query.data?.items ?? []);
  const scope = scopes.find((item) => item.config.id === selected) ?? scopes[0];
  return (
    <SettingsPage
      title="Agent 协调"
      description="管理主 Agent 的接单规则、模型和执行会话并发。"
      scope="团队级"
      actions={
        manager && <Button onClick={() => setSetup(true)}>新增协调范围</Button>
      }
    >
      {!manager ? (
        <Card>
          <p>需要团队所有者或管理员权限。任务与审批仍可从运行页面查看。</p>
        </Card>
      ) : (
        <>
          <SettingsLoading
            loading={query.isPending}
            error={query.error}
            retry={query.refetch}
          />
          {!!scopes.length && (
            <label className="form-field settings-fields">
              <span>协调范围</span>
              <Select
                aria-label="协调范围"
                value={scope?.config.id ?? ""}
                optionList={scopes.map((item) => ({
                  value: item.config.id,
                  label: `${item.config.source_kind === "code_hosting" ? "仓库" : "飞书"} · ${item.config.route_key || item.config.source_id || item.config.id}`,
                }))}
                onChange={(value) =>
                  env?.attempt(() => setSelected(String(value)))
                }
              />
            </label>
          )}
          {scope && (
            <CoordinationForm
              key={scope.config.id}
              initial={scope.config}
              workspace={workspace}
            />
          )}
          {!query.isPending && !query.error && !scopes.length && (
            <Card>
              <Empty
                title="尚未配置主 Agent"
                description="先登记飞书来源或仓库，再绑定主 Agent 与 Worker 的固定版本。"
              >
                <Button theme="solid" onClick={() => setSetup(true)}>
                  新增协调范围
                </Button>
              </Empty>
            </Card>
          )}
        </>
      )}
      <Link to="/coordination">查看协调状态与会话 →</Link>
      {setup && (
        <SetupDialog workspaceId={workspace} close={() => setSetup(false)} />
      )}
    </SettingsPage>
  );
}
function CoordinationForm({
  initial,
  workspace,
}: {
  initial: CoordinationConfig;
  workspace: string;
}) {
  const [baseline, setBaseline] = useState(initial);
  const [success, setSuccess] = useState<string | null>(null);
  const [values, setValues] = useState(initial),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const cache = useQueryClient();
  const dirty = JSON.stringify(values) !== JSON.stringify(baseline);
  useSettingsDirty(dirty);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await saveCoordination(workspace, values);
      const latest = await listCoordination(workspace);
      cache.setQueryData(["coordination", workspace], latest);
      const saved = latest.items.find((item) => item.config.id === values.id);
      if (!saved) throw new Error("配置已保存，但读取结果失败，请刷新后核对");
      setValues(saved.config);
      setBaseline(saved.config);
      setSuccess("协调配置已保存");
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="settings-stack" onSubmit={submit}>
      <Card title="接单与调度">
        <fieldset disabled={busy} className="settings-stack">
          <div className="settings-grid">
            <label className="form-field">
              <span>接单模式</span>
              <Select
                aria-label="接单模式"
                value={values.mode}
                optionList={[
                  { value: "off", label: "关闭自动接单" },
                  { value: "observe", label: "观察模式 · 仅记录判断" },
                  { value: "auto", label: "自动模式 · 按规则接单" },
                ]}
                onChange={(value) =>
                  setValues({
                    ...values,
                    mode: value as CoordinationConfig["mode"],
                  })
                }
              />
            </label>
            <label className="form-field">
              <span>运行状态</span>
              <Select
                aria-label="运行状态"
                value={values.paused ? "paused" : "active"}
                optionList={[
                  { value: "active", label: "正常调度" },
                  { value: "paused", label: "暂停调度" },
                ]}
                onChange={(value) =>
                  setValues({ ...values, paused: value === "paused" })
                }
              />
            </label>
            <label className="form-field">
              <span>执行会话并发上限</span>
              <Input
                type="number"
                min={1}
                max={10}
                required
                value={String(values.max_workers)}
                onChange={(value) =>
                  setValues({ ...values, max_workers: Number(value) })
                }
              />
              <small className="form-help">
                允许 1～10；运行中任务仍受各自审批与权限约束。
              </small>
            </label>
            <CoordinationModelField
              label="主 Agent 模型"
              value={values.main_model_id}
              onChange={(main_model_id) =>
                setValues({ ...values, main_model_id })
              }
            />
            <CoordinationModelField
              label="Worker 模型"
              value={values.worker_model_id}
              onChange={(worker_model_id) =>
                setValues({ ...values, worker_model_id })
              }
            />
          </div>
          <Checkbox
            checked={values.enabled}
            onChange={(event) =>
              setValues({ ...values, enabled: event.target.checked })
            }
          >
            启用此协调范围
          </Checkbox>
        </fieldset>
      </Card>
      <Card title="自动接单规则">
        <fieldset className="settings-stack" disabled={busy}>
          {values.rules.map((rule, index) => (
            <div className="settings-stack" key={rule.id}>
              <label className="form-field">
                <span>{rule.name}</span>
                <TextArea
                  required
                  maxLength={4000}
                  rows={3}
                  value={rule.instruction}
                  onChange={(instruction) =>
                    setValues({
                      ...values,
                      rules: values.rules.map((item, at) =>
                        at === index ? { ...item, instruction } : item,
                      ),
                    })
                  }
                />
              </label>
              <Checkbox
                checked={rule.enabled}
                onChange={(event) =>
                  setValues({
                    ...values,
                    rules: values.rules.map((item, at) =>
                      at === index
                        ? { ...item, enabled: event.target.checked }
                        : item,
                    ),
                  })
                }
              >
                启用此规则
              </Checkbox>
            </div>
          ))}
          <p className="form-help">
            自动模式下，规则判断通过后可创建任务；执行、推送与 MR
            审批保持独立。来源和准入范围沿用已有配置。
          </p>
        </fieldset>
      </Card>
      <SettingsResult error={error} success={success} />
      <div className="settings-actions">
        <Button
          theme="solid"
          htmlType="submit"
          loading={busy}
          disabled={!dirty}
        >
          保存协调配置
        </Button>
      </div>
    </form>
  );
}
