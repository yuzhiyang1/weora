import { Button, Table, Tag } from "../../components/console";
import type { Repository } from "../projects/api";

function connectionState(repository: Repository) {
  if (repository.status === "disabled")
    return { label: "已停用", color: "grey" };
  if (repository.connection_status === "connected")
    return { label: "读取检测通过", color: "green" };
  if (repository.connection_status === "failed")
    return { label: "读取检测失败", color: "red" };
  return { label: "已添加 · 未检测", color: "grey" };
}

/** 使用既有类型化表格展示仓库；检测和编辑权限仍由当前业务空间决定。 */
export function RepositorySettingsTable({
  repositories,
  canManage,
  checking,
  onCheck,
  onEdit,
}: {
  repositories: Repository[];
  canManage: boolean;
  checking: string | null;
  onCheck: (repository: Repository) => Promise<void>;
  onEdit: (repository: Repository) => void;
}) {
  return (
    <Table<Repository>
      variant="secondary"
      className="settings-repositories-table"
      rowKey="id"
      dataSource={repositories}
      pagination={false}
      columns={[
        {
          title: "仓库 / Git 地址",
          dataIndex: "name",
          render: (_, repository) => (
            <div className="settings-repository-copy">
              <strong>{repository.name}</strong>
              <p>{repository.git_url}</p>
            </div>
          ),
        },
        {
          title: "用途",
          dataIndex: "purpose",
          render: (value) => value || "未填写仓库说明",
        },
        { title: "分支", dataIndex: "default_branch" },
        {
          title: "读取状态",
          dataIndex: "connection_status",
          render: (_, repository) => {
            const status = connectionState(repository);
            return <Tag color={status.color}>{status.label}</Tag>;
          },
        },
        ...(canManage
          ? [
              {
                title: "操作",
                dataIndex: "id" as const,
                render: (_: string, repository: Repository) => (
                  <div className="settings-inline settings-table-actions">
                    <Button
                      theme="borderless"
                      size="small"
                      disabled={!!checking || repository.status === "disabled"}
                      loading={checking === repository.id}
                      onClick={() => void onCheck(repository)}
                    >
                      校验连接
                    </Button>
                    <Button
                      theme="borderless"
                      size="small"
                      disabled={!!checking}
                      onClick={() => onEdit(repository)}
                    >
                      编辑
                    </Button>
                  </div>
                ),
              },
            ]
          : []),
      ]}
    />
  );
}
