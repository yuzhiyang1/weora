import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Plus } from "lucide-react";
import { Button, Card, Modal } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import {
  getModelCatalog,
  modelConnectionAction,
  checkModelConnection,
  type ModelCatalog,
  type ModelConnection,
} from "./api";
import { ModelConnectionEditor } from "./ModelConnectionEditor";
import {
  ModelConnectionsTable,
  type ModelConnectionChecks,
} from "./ModelConnectionsTable";
import { ModelBudgetForm } from "./ModelBudgetForm";
import "./models.css";
import {
  SettingsPage,
  SettingsLoading,
  SettingsResult,
  useTeamManager,
} from "./SettingsPage";

export function ModelSettingsPage() {
  const { principal } = useAuth();
  const manager = useTeamManager();
  const workspace = principal!.workspace.id;
  const cache = useQueryClient();
  const queryKey = ["settings-model-catalog", workspace, principal!.user.id];
  const query = useQuery({
    queryKey,
    queryFn: () => getModelCatalog(workspace),
    enabled: manager,
  });
  const [editor, setEditor] = useState<ModelConnection | "new" | null>(null);
  const [removing, setRemoving] = useState<ModelConnection | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [checks, setChecks] = useState<ModelConnectionChecks>({});
  const [error, setError] = useState<unknown>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const catalog = query.isError ? undefined : query.data;

  function saved(result: ModelCatalog, message: string) {
    cache.setQueryData(queryKey, result);
    setSuccess(message);
    setError(null);
  }
  async function act(
    item: ModelConnection,
    action: "primary" | "remove" | "check",
  ) {
    if (busy || !catalog) return;
    setBusy(`${action}:${item.id}`);
    setError(null);
    setSuccess(null);
    try {
      if (action === "check") {
        // 开始重试就撤销旧成功标记，失败时不能继续显示此前的绿色状态。
        setChecks((previous) => {
          const next = { ...previous };
          delete next[item.id];
          return next;
        });
        await checkModelConnection(workspace, item.id, catalog.revision);
        setChecks((previous) => ({
          ...previous,
          [item.id]: { revision: catalog.revision, status: "passed" },
        }));
        setSuccess(`${item.name}：认证通过，所选模型可见。`);
      } else {
        saved(
          await modelConnectionAction(
            workspace,
            item.id,
            catalog.revision,
            action,
          ),
          action === "primary"
            ? `已将 ${item.name} 设为主要配置，新任务和新主会话将使用此配置。`
            : "备用配置已移除，历史运行保持原有版本。",
        );
        setRemoving(null);
      }
    } catch (cause) {
      if (action === "check") {
        setChecks((previous) => ({
          ...previous,
          [item.id]: { revision: catalog.revision, status: "failed" },
        }));
      }
      setError(cause);
    } finally {
      setBusy(null);
    }
  }
  return (
    <SettingsPage
      title="模型与密钥"
      description="保存多个模型与备用 Key，按需切换主要配置。"
      scope="团队级"
    >
      {!manager ? (
        <Card>
          <p>仅团队所有者和管理员可以查看与修改模型配置。</p>
        </Card>
      ) : (
        <>
          <SettingsLoading
            loading={query.isPending}
            error={query.error}
            retry={query.refetch}
          />
          {catalog && (
            <>
              <Card
                title="模型配置"
                description="每条配置保存一个厂商、模型和 Key。"
                className="model-connections-card"
                headerExtraContent={
                  <Button
                    icon={<Plus size={16} />}
                    theme="solid"
                    disabled={!!busy}
                    onClick={() => setEditor("new")}
                  >
                    添加配置
                  </Button>
                }
              >
                <div className="model-settings-scope">
                  <p>主要配置用于新任务和新主会话。</p>
                  <p>已有任务续接与重试沿用原配置版本。</p>
                </div>
                <ModelConnectionsTable
                  catalog={catalog}
                  checks={checks}
                  busy={busy}
                  onPrimary={(item) => void act(item, "primary")}
                  onCheck={(item) => void act(item, "check")}
                  onEdit={setEditor}
                  onRemove={(item) => {
                    setError(null);
                    setRemoving(item);
                  }}
                />
                <p className="model-settings-help">
                  Key
                  加密保存且不回显。检测仅验证认证与模型可见性，不验证剩余额度。
                </p>
              </Card>
              <SettingsResult
                error={removing ? null : error}
                success={success}
              />
              {!!error && !removing && (
                <Button disabled={!!busy} onClick={() => void query.refetch()}>
                  刷新列表
                </Button>
              )}
              <ModelBudgetForm
                catalog={catalog}
                workspace={workspace}
                blocked={!!busy}
                onBusy={(value) => setBusy(value ? "budget" : null)}
                onSaved={(result) =>
                  saved(
                    result,
                    "运行预算已保存，仅新任务和新主会话使用更新配置",
                  )
                }
              />
              <p className="settings-footnote model-settings-help">
                已有任务的续接和已有主会话沿用原版本，工具授权与审批规则保持不变。
              </p>
              {!catalog.runtime_enabled && (
                <p className="model-settings-help">
                  当前部署未启用执行器；保存配置不会启用执行权限。
                </p>
              )}
              {editor && (
                <ModelConnectionEditor
                  workspace={workspace}
                  catalog={catalog}
                  current={editor === "new" ? undefined : editor}
                  onClose={() => setEditor(null)}
                  onSaved={(result) => {
                    saved(result, "模型配置已保存");
                    setEditor(null);
                  }}
                />
              )}
              {removing && (
                <Modal
                  title="移除备用配置"
                  visible
                  onCancel={() => {
                    if (!busy) setRemoving(null);
                  }}
                  maskClosable={!busy}
                  closeOnEsc={!busy}
                  width={480}
                >
                  <p>
                    将从备用列表移除「{removing.name}
                    」。已固定的历史运行继续使用原有版本。
                  </p>
                  <SettingsResult error={error} />
                  <div className="modal-actions">
                    <Button disabled={!!busy} onClick={() => setRemoving(null)}>
                      取消
                    </Button>
                    <Button
                      type="danger"
                      loading={!!busy}
                      onClick={() => void act(removing, "remove")}
                    >
                      移除配置
                    </Button>
                  </div>
                </Modal>
              )}
            </>
          )}
        </>
      )}
    </SettingsPage>
  );
}
