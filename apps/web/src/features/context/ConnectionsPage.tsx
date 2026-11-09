import {
  Banner,
  Card,
  Spin,
  Button,
  Input,
  Modal,
  Select,
  Table,
  Tag,
  Typography,
} from "../../components/console";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { useAuth } from "../auth/AuthProvider";
import { errorMessage } from "../runs/api";
import {
  contextList,
  contextSave,
  type Binding,
  type Collection,
  type Connection,
  type Grant,
  type Source,
} from "./api";
import { ChannelStatusPanel } from "./ChannelStatusPanel";
import "./context.css";
import { useSettingsDirty } from "../settings/SettingsDrafts";
import { useDiscardChanges } from "../settings/useDiscardChanges";

type ConfigurationChange = { path: string; body: unknown; method?: string };
type ConfirmedChange = {
  title: string;
  description: string;
  change: ConfigurationChange;
};

export function ConnectionsPage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("上下文设置缺少当前身份");
  const { workspace, user } = principal;
  const manager = ["owner", "admin"].includes(workspace.role);
  return (
    <section className="asset-page context-page page-enter">
      <header className="page-header">
        <div>
          <span className="page-eyebrow">FEISHU / CONTEXT ACCESS</span>
          <Typography.Title heading={1}>飞书上下文</Typography.Title>
          <Typography.Paragraph>
            连接、身份关联、来源授权和投递相互独立。收到 @
            消息不代表已经获得历史读取权限。
          </Typography.Paragraph>
        </div>
      </header>
      <Banner
        className="boundary-banner"
        closeIcon={null}
        title="固定应用身份 · 最小授权"
        description="不使用个人登录态、不按显示名绑定成员。应用密钥仅写入后台；来源授权在每次读取、预览和交付时重新核验。"
        type="info"
      />
      <ContextConfiguration
        key={`${workspace.id}:${user.id}`}
        workspaceId={workspace.id}
        userId={user.id}
        manager={manager}
      />
      {manager ? (
        <ChannelStatusPanel workspaceId={workspace.id} userId={user.id} />
      ) : null}
    </section>
  );
}

export function ContextConfiguration({
  workspaceId,
  userId,
  manager,
  view = "all",
}: {
  workspaceId: string;
  userId: string;
  manager: boolean;
  view?: "all" | "apps" | "auth";
}) {
  const client = useQueryClient();
  const [confirmation, setConfirmation] = useState<ConfirmedChange | null>(
    null,
  );
  const baseKey = [
    "context-config",
    workspaceId,
    userId,
    manager ? "manage" : "reader",
  ];
  const connections = useQuery({
    queryKey: [...baseKey, "connections"],
    queryFn: () => contextList<Connection>(workspaceId, "connections"),
    enabled: manager,
    retry: false,
  });
  const sources = useQuery({
    queryKey: [...baseKey, "sources"],
    queryFn: () =>
      contextList<Source>(
        workspaceId,
        manager ? "sources?managed=true" : "sources",
      ),
    retry: false,
  });
  const grants = useQuery({
    queryKey: [...baseKey, "grants"],
    queryFn: () => contextList<Grant>(workspaceId, "grants"),
    enabled: manager,
    retry: false,
  });
  const bindings = useQuery({
    queryKey: [...baseKey, "bindings"],
    queryFn: () => contextList<Binding>(workspaceId, "bindings"),
    enabled: manager,
    retry: false,
  });
  const collections = useQuery({
    queryKey: [...baseKey, "collections"],
    queryFn: () => contextList<Collection>(workspaceId, "collections"),
    retry: false,
  });
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [appId, setAppId] = useState("");
  const [secret, setSecret] = useState("");
  const [rotating, setRotating] = useState<Connection | null>(null);
  const [replacementSecret, setReplacementSecret] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [chatId, setChatId] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [readerId, setReaderId] = useState("");
  const [openId, setOpenId] = useState("");
  const [memberId, setMemberId] = useState("");
  const [collectionName, setCollectionName] = useState("");
  const [collectionSources, setCollectionSources] = useState<string[]>([]);
  const mutate = useMutation({
    mutationFn: ({ path, body, method }: ConfigurationChange) =>
      contextSave(workspaceId, path, body, method),
    onSuccess: () => {
      setNotice("配置已保存；只有真实探测结果会被标为可用。");
      void client.invalidateQueries({ queryKey: baseKey });
    },
  });
  function requestConfirmation(value: ConfirmedChange) {
    if (mutate.isPending) return;
    setNotice(null);
    // 设置页先解释影响范围；旧上下文页沿用既有交互与服务端权限检查。
    if (view === "all") mutate.mutate(value.change);
    else {
      mutate.reset();
      setConfirmation(value);
    }
  }
  async function confirmChange() {
    if (!confirmation || mutate.isPending) return;
    try {
      await mutate.mutateAsync(confirmation.change);
      setConfirmation(null);
    } catch {
      // 保留确认内容及可重试入口，错误由弹窗统一展示。
    }
  }
  async function save(
    event: FormEvent,
    path: string,
    body: unknown,
    success?: () => void,
  ) {
    event.preventDefault();
    if (mutate.isPending) return;
    setNotice(null);
    try {
      await mutate.mutateAsync({ path, body });
      success?.();
    } catch {
      /* 错误统一在下方显示，不输出请求正文或密钥。 */
    } finally {
      if (path === "connections") {
        setSecret("");
        setReplacementSecret("");
      }
    }
  }
  const addDialog = useDiscardChanges(!!(name || appId || secret), () => {
    setAdding(false);
    setSecret("");
    setName("");
    setAppId("");
  });
  const rotateDialog = useDiscardChanges(!!replacementSecret, () => {
    setRotating(null);
    setReplacementSecret("");
  });
  const connectionOptions = (connections.data?.items ?? []).map((item) => ({
    label: item.name,
    value: item.id,
  }));
  const sourceOptions = (sources.data?.items ?? []).map((item) => ({
    label: item.name,
    value: item.id,
  }));
  const errors = [
    connections.error,
    sources.error,
    grants.error,
    bindings.error,
    collections.error,
  ].filter(Boolean);

  useSettingsDirty(
    !!(
      name ||
      appId ||
      secret ||
      replacementSecret ||
      chatId ||
      sourceName ||
      readerId ||
      openId ||
      memberId ||
      collectionName ||
      collectionSources.length
    ),
  );
  // 已显示过的数据在撤权/连接失效时也不可作为缓存继续预览。
  if (errors.length)
    return (
      <Banner
        closeIcon={null}
        title="上下文配置尚不可用"
        description={errorMessage(errors[0])}
        type="warning"
      />
    );

  if (
    sources.isPending ||
    collections.isPending ||
    (manager && connections.isPending)
  )
    return <Spin tip="正在加载飞书配置…" />;

  return (
    <div className="context-stack">
      {errors.length ? (
        <Banner
          closeIcon={null}
          title="上下文配置尚不可用"
          description={errorMessage(errors[0])}
          type="warning"
        />
      ) : null}
      {mutate.isError ? (
        <Banner
          closeIcon={null}
          description={errorMessage(mutate.error)}
          type="danger"
        />
      ) : null}
      {notice ? (
        <Banner closeIcon={null} description={notice} type="info" />
      ) : null}
      {manager && view !== "auth" ? (
        <Card>
          <div className="settings-row">
            <Typography.Title heading={3}>应用连接</Typography.Title>
            <Button theme="solid" onClick={() => setAdding(true)}>
              添加应用
            </Button>
          </div>
          {(connections.data?.items.length ?? 0) > 0 ? (
            <Table
              variant={view === "all" ? "primary" : "secondary"}
              rowKey="id"
              pagination={false}
              dataSource={connections.data?.items ?? []}
              columns={[
                {
                  title: "名称 / 应用",
                  dataIndex: "name",
                  render: (_: unknown, row: Connection) => (
                    <span>
                      {row.name}
                      <small className="context-secondary">{row.app_id}</small>
                    </span>
                  ),
                },
                {
                  title: "连接",
                  dataIndex: "connection_status",
                  render: (value: string) => <Tag>{statusLabel(value)}</Tag>,
                },
                {
                  title: "事件配置",
                  dataIndex: "event_status",
                  render: (value: string) => <Tag>{statusLabel(value)}</Tag>,
                },
                {
                  title: "状态",
                  dataIndex: "enabled",
                  render: (value: boolean) => (value ? "启用" : "禁用"),
                },
                {
                  title: "操作",
                  dataIndex: "id",
                  render: (_: unknown, row: Connection) => (
                    <div className="run-actions">
                      <Button
                        disabled={mutate.isPending}
                        onClick={() =>
                          mutate.mutate({
                            path: `connections/${row.id}/probe`,
                            body: {},
                          })
                        }
                      >
                        探测连接
                      </Button>
                      <Button
                        onClick={() => {
                          setRotating(row);
                          setReplacementSecret("");
                        }}
                      >
                        更新密钥
                      </Button>
                      <Button
                        type={row.enabled ? "danger" : "primary"}
                        disabled={mutate.isPending}
                        onClick={() =>
                          row.enabled
                            ? requestConfirmation({
                                title: "停用飞书应用",
                                description: `将停用「${row.name}」，影响此应用关联来源的后续访问。历史记录保留。`,
                                change: {
                                  path: "connections",
                                  body: {
                                    connection_id: row.id,
                                    app_id: row.app_id,
                                    name: row.name,
                                    enabled: false,
                                  },
                                },
                              })
                            : mutate.mutate({
                                path: "connections",
                                body: {
                                  connection_id: row.id,
                                  app_id: row.app_id,
                                  name: row.name,
                                  enabled: true,
                                },
                              })
                        }
                      >
                        {row.enabled ? "禁用连接" : "启用连接"}
                      </Button>
                    </div>
                  ),
                },
              ]}
            />
          ) : (
            <p className="form-help">
              尚未添加应用连接。点击“添加应用”配置固定的机器人身份。
            </p>
          )}
          <Modal
            title="添加飞书应用"
            visible={adding}
            onCancel={() => {
              if (!mutate.isPending) addDialog.requestClose();
            }}
            maskClosable={!mutate.isPending}
            closeOnEsc={!mutate.isPending}
            footer={null}
          >
            <form
              className="agent-form"
              onSubmit={(event) =>
                void save(
                  event,
                  "connections",
                  { name: name.trim(), app_id: appId.trim(), secret },
                  () => {
                    setSecret("");
                    setName("");
                    setAppId("");
                    setAdding(false);
                  },
                )
              }
            >
              <label className="form-field">
                <span>连接名称</span>
                <Input required value={name} onChange={setName} />
              </label>
              <label className="form-field">
                <span>App ID</span>
                <Input
                  required
                  value={appId}
                  onChange={setAppId}
                  placeholder="cli_..."
                />
              </label>
              <label className="form-field">
                <span>应用密钥（仅写）</span>
                <Input
                  mode="password"
                  required
                  value={secret}
                  onChange={setSecret}
                  autoComplete="new-password"
                />
              </label>
              <div className="modal-actions">
                <Button
                  disabled={mutate.isPending}
                  onClick={addDialog.requestClose}
                >
                  取消
                </Button>
                <Button
                  htmlType="submit"
                  loading={mutate.isPending}
                  disabled={!name.trim() || !appId.trim() || !secret}
                  theme="solid"
                >
                  保存新连接
                </Button>
              </div>
            </form>
            {mutate.isError && (
              <Banner type="danger" description={errorMessage(mutate.error)} />
            )}
          </Modal>
        </Card>
      ) : null}
      {view !== "auth" && (
        <Card>
          <Typography.Title heading={3}>
            {manager ? "群来源与独立探测" : "我可读取的群来源"}
          </Typography.Title>
          {(sources.data?.items.length ?? 0) > 0 ? (
            <Table
              variant={view === "all" ? "primary" : "secondary"}
              rowKey="id"
              pagination={false}
              dataSource={sources.data?.items ?? []}
              columns={[
                {
                  title: "来源",
                  dataIndex: "name",
                  render: (_: unknown, row: Source) => (
                    <span>
                      {row.name}
                      <small className="context-secondary">{row.chat_id}</small>
                    </span>
                  ),
                },
                {
                  title: "机器人入群",
                  dataIndex: "bot_membership",
                  render: (value: string) => statusLabel(value),
                },
                {
                  title: "历史读取",
                  dataIndex: "history_status",
                  render: (value: string) => statusLabel(value),
                },
                ...(manager
                  ? [
                      {
                        title: "操作",
                        dataIndex: "id" as const,
                        render: (_: unknown, row: Source) => (
                          <div className="run-actions">
                            <Button
                              onClick={() =>
                                mutate.mutate({
                                  path: `connections/${row.connection_id}/probe?source_id=${row.id}`,
                                  body: {},
                                })
                              }
                            >
                              探测群历史
                            </Button>
                            <Button
                              type="danger"
                              onClick={() =>
                                mutate.mutate({
                                  path: "sources",
                                  body: {
                                    connection_id: row.connection_id,
                                    chat_id: row.chat_id,
                                    name: row.name,
                                    source_id: row.id,
                                    enabled: !row.enabled,
                                  },
                                })
                              }
                            >
                              {row.enabled ? "停用来源" : "启用来源"}
                            </Button>
                          </div>
                        ),
                      },
                    ]
                  : []),
              ]}
            />
          ) : (
            <p className="form-help">
              尚未登记群来源。先添加应用，再填写群 ID 与来源名称。
            </p>
          )}
          {manager ? (
            <form
              className="context-form"
              onSubmit={(event) =>
                void save(
                  event,
                  "sources",
                  {
                    connection_id: connectionId,
                    chat_id: chatId.trim(),
                    name: sourceName.trim(),
                  },
                  () => {
                    setChatId("");
                    setSourceName("");
                  },
                )
              }
            >
              <div className="form-field">
                <span id="context-connection-label">来源连接</span>
                <Select
                  aria-labelledby="context-connection-label"
                  value={connectionId || undefined}
                  optionList={connectionOptions}
                  onChange={(value) => setConnectionId(String(value))}
                />
              </div>
              <label className="form-field">
                <span>群 ID</span>
                <Input
                  required
                  value={chatId}
                  onChange={setChatId}
                  placeholder="oc_..."
                />
              </label>
              <label className="form-field">
                <span>来源名称</span>
                <Input required value={sourceName} onChange={setSourceName} />
              </label>
              <Button
                theme="solid"
                htmlType="submit"
                disabled={!connectionId || !chatId.trim() || !sourceName.trim()}
              >
                登记群来源
              </Button>
            </form>
          ) : null}
        </Card>
      )}
      {manager && view !== "apps" ? (
        <div className="detail-columns settings-authorization-grid">
          <Card>
            <Typography.Title heading={3}>读者授权</Typography.Title>
            <p>
              填入已核验的工作空间成员 ID；关联飞书身份不会自动授予这里的权限。
            </p>
            <form
              className="agent-form"
              onSubmit={(event) => {
                event.preventDefault();
                mutate.mutate(
                  {
                    path: `sources/${sourceId}/grants/${readerId.trim()}`,
                    body: { enabled: true },
                    method: "PUT",
                  },
                  { onSuccess: () => setReaderId("") },
                );
              }}
            >
              <div className="form-field">
                <span id="context-grant-label">授权来源</span>
                <Select
                  aria-labelledby="context-grant-label"
                  value={sourceId || undefined}
                  optionList={sourceOptions}
                  onChange={(value) => setSourceId(String(value))}
                />
              </div>
              <label className="form-field">
                <span>读者成员 ID</span>
                <Input required value={readerId} onChange={setReaderId} />
              </label>
              <Button
                theme="solid"
                htmlType="submit"
                disabled={!sourceId || !readerId.trim()}
              >
                授予读取权限
              </Button>
            </form>
            <ul className="context-records">
              {grants.data?.items.map((item) => (
                <li key={`${item.source_id}:${item.user_id}`}>
                  <span>
                    {item.user_id}
                    <small>
                      {item.source_id} · {item.enabled ? "已授权" : "已撤销"}
                    </small>
                  </span>
                  {item.enabled ? (
                    <Button
                      type="danger"
                      onClick={() =>
                        requestConfirmation({
                          title: "撤销来源读取权限",
                          description:
                            "仅撤销该成员对指定来源的读取授权。身份映射与来源集合不会授予额外权限。",
                          change: {
                            path: `sources/${item.source_id}/grants/${item.user_id}`,
                            body: { enabled: false },
                            method: "PUT",
                          },
                        })
                      }
                    >
                      撤权
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          </Card>
          <Card>
            <Typography.Title heading={3}>成员身份关联</Typography.Title>
            <p>只匹配明确的 connection + open_id，不以姓名猜测身份。</p>
            <form
              className="agent-form settings-identity-form"
              onSubmit={(event) =>
                void save(
                  event,
                  "bindings",
                  {
                    connection_id: connectionId,
                    open_id: openId.trim(),
                    user_id: memberId.trim(),
                    enabled: true,
                  },
                  () => {
                    setOpenId("");
                    setMemberId("");
                  },
                )
              }
            >
              <div className="form-field">
                <span id="context-binding-label">身份连接</span>
                <Select
                  aria-labelledby="context-binding-label"
                  value={connectionId || undefined}
                  optionList={connectionOptions}
                  onChange={(value) => setConnectionId(String(value))}
                />
              </div>
              <label className="form-field">
                <span>飞书 Open ID</span>
                <Input
                  required
                  value={openId}
                  onChange={setOpenId}
                  placeholder="ou_..."
                />
              </label>
              <label className="form-field">
                <span>Weora 成员 ID</span>
                <Input required value={memberId} onChange={setMemberId} />
              </label>
              <Button
                theme="solid"
                htmlType="submit"
                disabled={!connectionId || !openId.trim() || !memberId.trim()}
              >
                关联明确身份
              </Button>
            </form>
            <ul className="context-records">
              {bindings.data?.items.map((item) => (
                <li key={`${item.connection_id}:${item.open_id}`}>
                  <span>
                    {item.open_id}
                    <small>
                      {item.user_id} · {item.enabled ? "已关联" : "已解除"}
                    </small>
                  </span>
                  {item.enabled ? (
                    <Button
                      type="danger"
                      onClick={() =>
                        mutate.mutate({
                          path: "bindings",
                          body: { ...item, enabled: false },
                        })
                      }
                    >
                      解除关联
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      ) : null}
      {view !== "apps" && (
        <Card>
          <Typography.Title heading={3}>上下文来源集合</Typography.Title>
          <p>集合只组织来源，不扩大任何成员的原有读取权限。</p>
          <ul>
            {collections.data?.items.map((item) => (
              <li key={item.id}>
                {item.name} · {item.source_ids.length} 个可见来源
              </li>
            ))}
          </ul>
          {manager ? (
            <form
              className="context-form settings-collection-form"
              onSubmit={(event) =>
                void save(
                  event,
                  "collections",
                  {
                    name: collectionName.trim(),
                    source_ids: collectionSources,
                  },
                  () => {
                    setCollectionName("");
                    setCollectionSources([]);
                  },
                )
              }
            >
              <label className="form-field">
                <span>集合名称</span>
                <Input
                  required
                  value={collectionName}
                  onChange={setCollectionName}
                />
              </label>
              <div className="form-field">
                <span id="context-collection-label">集合来源</span>
                <Select
                  aria-labelledby="context-collection-label"
                  multiple
                  value={collectionSources}
                  optionList={sourceOptions}
                  onChange={(value) => setCollectionSources(value as string[])}
                />
              </div>
              <Button
                theme="solid"
                htmlType="submit"
                disabled={!collectionName.trim()}
              >
                保存来源集合
              </Button>
            </form>
          ) : null}
        </Card>
      )}
      <Modal
        centered
        title="覆盖应用密钥"
        visible={!!rotating}
        footer={null}
        maskClosable={!mutate.isPending}
        onCancel={() => {
          if (!mutate.isPending) rotateDialog.requestClose();
        }}
      >
        <form
          className="agent-form"
          onSubmit={(event) => {
            if (!rotating) return;
            void save(
              event,
              "connections",
              {
                connection_id: rotating.id,
                app_id: rotating.app_id,
                name: rotating.name,
                enabled: rotating.enabled,
                secret: replacementSecret,
              },
              () => {
                setRotating(null);
                setReplacementSecret("");
              },
            );
          }}
        >
          <p>
            只覆盖此连接的密钥，不读取旧密钥，也不切换应用身份。更新后需要重新探测。
          </p>
          <label className="form-field">
            <span>新应用密钥（仅写）</span>
            <Input
              mode="password"
              autoComplete="new-password"
              required
              value={replacementSecret}
              onChange={setReplacementSecret}
            />
          </label>
          {mutate.isError ? (
            <Banner
              closeIcon={null}
              description={errorMessage(mutate.error)}
              type="danger"
            />
          ) : null}
          <div className="modal-actions">
            <Button
              disabled={mutate.isPending}
              onClick={() => {
                setRotating(null);
                setReplacementSecret("");
              }}
            >
              取消
            </Button>
            <Button
              htmlType="submit"
              disabled={!replacementSecret}
              loading={mutate.isPending}
              theme="solid"
            >
              覆盖并重新验证
            </Button>
          </div>
        </form>
      </Modal>
      {addDialog.confirmation}
      {rotateDialog.confirmation}
      {confirmation && (
        <Modal
          title={confirmation.title}
          visible
          width={480}
          onCancel={() => {
            if (!mutate.isPending) setConfirmation(null);
          }}
          maskClosable={!mutate.isPending}
          closeOnEsc={!mutate.isPending}
        >
          <p>{confirmation.description}</p>
          {mutate.error && (
            <Banner type="danger" description={errorMessage(mutate.error)} />
          )}
          <div className="modal-actions">
            <Button
              disabled={mutate.isPending}
              onClick={() => setConfirmation(null)}
            >
              取消
            </Button>
            <Button
              type="danger"
              loading={mutate.isPending}
              onClick={() => void confirmChange()}
            >
              确认
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

export function statusLabel(value: string | undefined) {
  return (
    (
      {
        unverified: "尚未验证",
        available: "可用",
        connected: "已连接",
        ready: "连接就绪",
        verified: "已确认入群",
        readable: "历史可读",
        received: "已收到事件",
        not_in_group: "机器人尚未入群",
        configured: "已配置",
        unavailable: "不可用",
        denied: "无权限",
        disabled: "已禁用",
        unknown: "未知",
      } as Record<string, string>
    )[value ?? ""] ??
    value ??
    "尚未验证"
  );
}
