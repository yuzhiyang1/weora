import { Dropdown } from "@heroui/react";
import { Button, Empty, Table, Tag } from "../../components/console";
import type { ModelCatalog, ModelConnection } from "./api";

export type ModelConnectionChecks = Record<
  string,
  {
    revision: number;
    status: "passed" | "failed";
  }
>;

/** 同一份表格在中窄屏按行堆叠，避免渲染两套控件造成焦点与菜单状态分叉。 */
export function ModelConnectionsTable({
  catalog,
  checks,
  busy,
  onPrimary,
  onCheck,
  onEdit,
  onRemove,
}: {
  catalog: ModelCatalog;
  checks: ModelConnectionChecks;
  busy: string | null;
  onPrimary: (item: ModelConnection) => void;
  onCheck: (item: ModelConnection) => void;
  onEdit: (item: ModelConnection) => void;
  onRemove: (item: ModelConnection) => void;
}) {
  const connections = [...catalog.items].sort(
    (first, second) =>
      Number(second.id === catalog.primary_id) -
      Number(first.id === catalog.primary_id),
  );

  if (!connections.length) {
    return (
      <Empty
        title="还没有模型配置"
        description="添加第一个模型和 Key，开始使用 Agent。"
      />
    );
  }

  return (
    <Table<ModelConnection>
      className="model-connections-table"
      variant="secondary"
      rowKey="id"
      dataSource={connections}
      pagination={false}
      columns={[
        {
          title: "配置名称",
          dataIndex: "name",
          render: (_, item) => (
            <div className="model-connection-name">
              <strong>{item.name}</strong>
              {catalog.primary_id === item.id && <Tag color="blue">主要</Tag>}
            </div>
          ),
        },
        {
          title: "厂商",
          dataIndex: "provider",
          render: (provider) =>
            catalog.providers.find((item) => item.id === provider)?.label ??
            provider,
        },
        { title: "模型", dataIndex: "model_id" },
        {
          title: "Key 状态",
          dataIndex: "has_key",
          render: (_, item) => {
            const result = checks[item.id];
            const status =
              result?.revision === catalog.revision ? result.status : undefined;
            const verified = status === "passed";
            const failed = status === "failed";
            return (
              <Tag color={failed ? "red" : verified ? "green" : "grey"}>
                {!item.has_key
                  ? "未配置密钥"
                  : failed
                    ? "检测失败"
                    : verified
                      ? "认证通过 · 模型可见"
                      : "已配置 · 未检测"}
              </Tag>
            );
          },
        },
        {
          title: "操作",
          dataIndex: "id",
          render: (_, item) => (
            <div className="model-connection-actions">
              {catalog.primary_id !== item.id && (
                <Button
                  disabled={!!busy || !item.has_key}
                  loading={busy === `primary:${item.id}`}
                  onClick={() => onPrimary(item)}
                >
                  设为主要
                </Button>
              )}
              <Button disabled={!!busy} onClick={() => onEdit(item)}>
                编辑
              </Button>
              <Dropdown>
                <Button
                  theme="light"
                  disabled={!!busy}
                  loading={busy === `check:${item.id}`}
                  aria-label={`更多：${item.name}`}
                >
                  更多
                </Button>
                <Dropdown.Popover placement="bottom end">
                  <Dropdown.Menu aria-label={`${item.name}的更多操作`}>
                    <Dropdown.Item
                      id="check"
                      isDisabled={!!busy || !item.has_key}
                      onAction={() => onCheck(item)}
                    >
                      检测连接
                    </Dropdown.Item>
                    {catalog.primary_id !== item.id && (
                      <Dropdown.Item
                        id="remove"
                        variant="danger"
                        isDisabled={!!busy}
                        onAction={() => onRemove(item)}
                      >
                        移除配置
                      </Dropdown.Item>
                    )}
                  </Dropdown.Menu>
                </Dropdown.Popover>
              </Dropdown>
            </div>
          ),
        },
      ]}
    />
  );
}
