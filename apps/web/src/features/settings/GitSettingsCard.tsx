import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Switch, Label } from "@heroui/react";
import { Button, Card, Input, Select, Tag } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { getGitSettings, saveGitSettings, type GitSettings } from "./api";
import { useSettingsDirty } from "./SettingsDrafts";
import {
  SettingsLoading,
  SettingsResult,
  useTeamManager,
} from "./SettingsPage";

export function GitPreferencesForm() {
  const { principal } = useAuth();
  const manager = useTeamManager();
  const workspace = principal!.workspace.id;
  const query = useQuery({
    queryKey: ["settings-git", workspace, principal!.user.id],
    queryFn: () => getGitSettings(workspace),
  });
  return (
    <Card title="全局 Git 设置" headerExtraContent={<Tag>团队级</Tag>}>
      <p className="form-help">
        团队默认的提交署名，新任务创建时冻结。执行环境默认身份为
        Weora，不读取宿主个人配置。
      </p>
      <SettingsLoading
        loading={query.isPending}
        error={query.error}
        retry={query.refetch}
      />
      {query.data && !query.isError && (
        <GitForm initial={query.data} workspace={workspace} manager={manager} />
      )}
    </Card>
  );
}
function GitForm({
  initial,
  workspace,
  manager,
}: {
  initial: GitSettings;
  workspace: string;
  manager: boolean;
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
      const result = await saveGitSettings(workspace, values);
      setValues(result);
      setBaseline(result);
      setSuccess("Git 设置已保存，仅新任务使用更新后的署名");
      cache.setQueriesData({ queryKey: ["settings-git", workspace] }, result);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="settings-stack" onSubmit={submit}>
      <fieldset className="settings-stack" disabled={!manager || busy}>
        <div className="settings-row">
          <div>
            <strong>提交人名称</strong>
            <p className="form-help">
              选择新任务的 Git author 与 committer 名称。
            </p>
          </div>
          <div className="settings-select">
            <Select
              aria-label="提交人名称"
              value={values.name_mode}
              onChange={(value) =>
                setValues({
                  ...values,
                  name_mode: value as GitSettings["name_mode"],
                })
              }
              optionList={[
                { value: "actor_agent", label: "任务发起人 & Agent 名称" },
                { value: "actor", label: "任务发起人名称" },
                { value: "environment", label: "执行环境默认身份" },
                { value: "manual", label: "手动设置" },
              ]}
            />
          </div>
        </div>
        {values.name_mode === "manual" && (
          <label className="form-field settings-fields">
            <span>提交名称</span>
            <Input
              required
              maxLength={160}
              value={values.manual_name}
              onChange={(manual_name) => setValues({ ...values, manual_name })}
            />
          </label>
        )}
        <div className="settings-row">
          <div>
            <strong>提交人邮箱</strong>
            <p className="form-help">任务发起人的邮箱来自已认证账户。</p>
          </div>
          <div className="settings-select">
            <Select
              aria-label="提交人邮箱"
              value={values.email_mode}
              onChange={(value) =>
                setValues({
                  ...values,
                  email_mode: value as GitSettings["email_mode"],
                })
              }
              optionList={[
                { value: "actor", label: "任务发起人邮箱" },
                { value: "environment", label: "执行环境默认邮箱" },
                { value: "manual", label: "手动设置" },
              ]}
            />
          </div>
        </div>
        {values.email_mode === "manual" && (
          <label className="form-field settings-fields">
            <span>提交邮箱</span>
            <Input
              required
              type="email"
              value={values.manual_email}
              onChange={(manual_email) =>
                setValues({ ...values, manual_email })
              }
            />
          </label>
        )}
        <div className="settings-row">
          <div>
            <strong>Co-authored-by trailer</strong>
            <p className="form-help">在 Agent 创建的提交中记录协作署名。</p>
          </div>
          <Switch
            aria-label="Co-authored-by trailer"
            isSelected={values.co_authored_by}
            onChange={(co_authored_by) =>
              setValues({ ...values, co_authored_by })
            }
          >
            <Switch.Content>
              <Switch.Control>
                <Switch.Thumb />
              </Switch.Control>
            </Switch.Content>
            <Label className="sr-only">Co-authored-by trailer</Label>
          </Switch>
        </div>
      </fieldset>
      <SettingsResult error={error} success={success} />
      {manager ? (
        <div className="settings-actions">
          <Button
            theme="solid"
            htmlType="submit"
            loading={busy}
            disabled={!dirty}
          >
            保存 Git 设置
          </Button>
        </div>
      ) : (
        <p className="form-help">仅团队所有者和管理员可修改全局 Git 设置。</p>
      )}
    </form>
  );
}
