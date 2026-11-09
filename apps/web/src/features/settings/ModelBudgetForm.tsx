import { useState, type FormEvent } from "react";
import { Button, Card, Input } from "../../components/console";
import { saveModelBudget, type ModelBudget, type ModelCatalog } from "./api";
import { useSettingsDirty } from "./SettingsDrafts";
import { SettingsResult } from "./SettingsPage";

function sameBudget(first: ModelBudget, second: ModelBudget) {
  return (
    first.max_tokens === second.max_tokens &&
    first.timeout_seconds === second.timeout_seconds &&
    first.execution_timeout_seconds === second.execution_timeout_seconds
  );
}

export function ModelBudgetForm({
  catalog,
  workspace,
  blocked,
  onBusy,
  onSaved,
}: {
  catalog: ModelCatalog;
  workspace: string;
  blocked: boolean;
  onBusy: (value: boolean) => void;
  onSaved: (result: ModelCatalog) => void;
}) {
  const [draft, setDraft] = useState<{
    baseline: ModelBudget;
    effectiveBaseline: ModelCatalog["effective_budget"];
    values: ModelBudget;
    revision: number;
  } | null>(null);
  const values = draft?.values ?? catalog.budget;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const dirty = draft !== null;
  useSettingsDirty(dirty);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || blocked || !draft) return;
    setBusy(true);
    onBusy(true);
    setError(null);
    try {
      // 只在预算基线没变时换用新目录版本；别人的预算更新必须由 CAS 提示冲突。
      const revision =
        sameBudget(catalog.budget, draft.baseline) &&
        sameBudget(catalog.effective_budget, draft.effectiveBaseline)
          ? catalog.revision
          : draft.revision;
      const saved = await saveModelBudget(workspace, revision, draft.values);
      setDraft(null);
      onSaved(saved);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
      onBusy(false);
    }
  }
  function field(label: string, key: keyof ModelBudget, maximum: number) {
    return (
      <label className="form-field">
        <span>{label}</span>
        <Input
          type="number"
          min={1}
          max={maximum}
          placeholder="使用部署默认值"
          value={values[key] === null ? "" : String(values[key])}
          onChange={(value) => {
            const baseline = draft?.baseline ?? catalog.budget;
            // 空值表达继承默认预算，不能被 Number("") 转成禁止执行的 0。
            const next = {
              ...values,
              [key]: value === "" ? null : Number(value),
            };
            setDraft(
              sameBudget(next, baseline)
                ? null
                : {
                    baseline,
                    effectiveBaseline:
                      draft?.effectiveBaseline ?? catalog.effective_budget,
                    values: next,
                    revision: draft?.revision ?? catalog.revision,
                  },
            );
          }}
        />
      </label>
    );
  }
  return (
    <form className="settings-stack" onSubmit={submit}>
      <Card
        title="运行预算"
        description="团队共用，与当前选择的 Key 分开保存。"
        className="model-budget-card"
      >
        <fieldset
          disabled={busy || blocked}
          className="settings-grid model-budget-grid"
        >
          {field("单次模型超时（秒）", "timeout_seconds", 600)}
          {field("整次执行超时（秒）", "execution_timeout_seconds", 3600)}
          {field("最大输出 Token", "max_tokens", 16384)}
        </fieldset>
        <p className="model-settings-help">
          可单独留空，使用保存时的部署默认值。已有任务及其续接保留固定预算。
        </p>
        <SettingsResult error={error} />
        <div className="settings-actions">
          <Button
            htmlType="submit"
            theme="solid"
            loading={busy}
            disabled={blocked || !dirty}
          >
            保存运行预算
          </Button>
        </div>
      </Card>
    </form>
  );
}
