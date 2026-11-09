import { useState, type FormEvent } from "react";
import {
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
} from "../../components/console";
import {
  saveModelConnection,
  type ModelCatalog,
  type ModelConnection,
} from "./api";
import { useDiscardChanges } from "./useDiscardChanges";
import { SettingsResult } from "./SettingsPage";

/** Key 只保存在本次弹窗；旧 Key 不回填，换厂商时也不能沿用输入中的旧凭据。 */
export function ModelConnectionEditor({
  workspace,
  catalog,
  current,
  onClose,
  onSaved,
}: {
  workspace: string;
  catalog: ModelCatalog;
  current?: ModelConnection | undefined;
  onClose: () => void;
  onSaved: (catalog: ModelCatalog) => void;
}) {
  const [name, setName] = useState(current?.name ?? "");
  const [provider, setProvider] = useState(
    current?.provider ?? catalog.providers[0]?.id ?? "",
  );
  const [model, setModel] = useState(current?.model_id ?? "");
  const [key, setKey] = useState("");
  const [clear, setClear] = useState(false);
  const primary = catalog.primary_id === current?.id;
  const [makePrimary, setMakePrimary] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [revision] = useState(catalog.revision);
  const [baseline] = useState(() => JSON.stringify({ name, provider, model }));
  const dirty =
    JSON.stringify({ name, provider, model }) !== baseline ||
    !!key ||
    clear ||
    makePrimary;
  const { requestClose, confirmation } = useDiscardChanges(dirty, onClose);
  const selectedProvider = catalog.providers.find(
    (item) => item.id === provider,
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await saveModelConnection(
        workspace,
        {
          revision,
          name: name.trim(),
          provider,
          model_id: model,
          make_primary: makePrimary,
          ...(clear ? { api_key: "" } : key ? { api_key: key } : {}),
        },
        current?.id,
      );
      onSaved(result);
    } catch (cause) {
      setError(cause);
    } finally {
      setKey("");
      setBusy(false);
    }
  }
  return (
    <>
      <Modal
        title={current ? "编辑模型配置" : "添加模型配置"}
        visible
        width={640}
        onCancel={() => {
          if (!busy) requestClose();
        }}
        maskClosable={!busy}
        closeOnEsc={!busy}
      >
        <form
          className="settings-stack model-connection-editor"
          onSubmit={submit}
        >
          <p className="form-help">
            {current
              ? "更换 Key 时填写新值；留空保留已保存的 Key。"
              : "为同一模型添加备用 Key，或连接另一个厂商。"}
          </p>
          <fieldset className="settings-stack" disabled={busy}>
            <div className="settings-grid">
              <label className="form-field">
                <span>配置名称</span>
                <Input
                  required
                  maxLength={120}
                  value={name}
                  onChange={setName}
                  placeholder="例如研发主 Key、备用 Key"
                />
              </label>
              <div className="form-field">
                <span>厂商</span>
                <Select
                  aria-label="厂商"
                  value={provider}
                  optionList={catalog.providers.map((item) => ({
                    value: item.id,
                    label: item.label,
                  }))}
                  onChange={(value) => {
                    if (String(value) === provider) return;
                    setProvider(String(value));
                    setModel("");
                    setKey("");
                    setClear(false);
                  }}
                />
              </div>
            </div>
            <div className="form-field">
              <span>模型</span>
              <Select
                aria-label="模型"
                value={model}
                placeholder="选择模型"
                optionList={(selectedProvider?.models ?? []).map((item) => ({
                  value: item.id,
                  label: `${item.name} · ${item.id}`,
                }))}
                onChange={(value) => setModel(String(value))}
              />
            </div>
            <label className="form-field">
              <span>API Key（只写）</span>
              <Input
                type="password"
                autoComplete="new-password"
                maxLength={4096}
                required={!current || current.provider !== provider}
                disabled={clear}
                value={key}
                onChange={setKey}
                placeholder={
                  current?.has_key
                    ? "留空保留已保存的 Key"
                    : "填写该厂商的 API Key"
                }
              />
            </label>
            <p className="form-help">
              已保存的 Key
              不回显。厂商地址由平台预设；同一模型可使用不同名称保存多条 Key。
            </p>
            {current?.has_key && !primary && current.provider === provider && (
              <Checkbox
                checked={clear}
                onChange={(event) => setClear(event.target.checked)}
              >
                清除已保存的 Key
              </Checkbox>
            )}
            {!primary && (
              <Checkbox
                checked={makePrimary}
                onChange={(event) => setMakePrimary(event.target.checked)}
              >
                保存后设为主要配置
              </Checkbox>
            )}
          </fieldset>
          <SettingsResult error={error} />
          <div className="modal-actions">
            <Button disabled={busy} onClick={requestClose}>
              取消
            </Button>
            <Button
              htmlType="submit"
              theme="solid"
              loading={busy}
              disabled={!name.trim() || !model || (current ? !dirty : !key)}
            >
              保存配置
            </Button>
          </div>
        </form>
      </Modal>
      {confirmation}
    </>
  );
}
