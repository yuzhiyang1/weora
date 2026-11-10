import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Ellipsis, UserPlus } from "lucide-react";
import { Dropdown } from "@heroui/react";
import { useState } from "react";
import {
  Avatar,
  Banner,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Select,
  TabPane,
  Tabs,
  Tag,
} from "../../components/console";
import { AuthApiError } from "../auth/api";
import { useAuth } from "../auth/AuthProvider";
import {
  copyInvitation,
  createInvitation,
  invitationUrl,
  listInvitations,
  listMembers,
  removeMember,
  revokeInvitation,
  roleName,
  type InviteDuration,
  type WorkspaceInvite,
  type WorkspaceMember,
} from "../auth/workspace-api";
import { SettingsLoading, SettingsPage, useTeamManager } from "./SettingsPage";
import "./workspace-members.css";

type Dialog =
  | { kind: "create" }
  | { kind: "link"; record: WorkspaceInvite; url: string; created: boolean }
  | { kind: "revoke"; record: WorkspaceInvite }
  | { kind: "remove"; member: WorkspaceMember };

const formatTime = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "short",
});
const statusLabel = {
  pending: "待使用",
  used: "已使用",
  revoked: "已撤销",
  expired: "已过期",
};
const statusTone = {
  pending: "blue",
  used: "green",
  revoked: "grey",
  expired: "grey",
};
const dialogTitles = {
  create: "邀请成员",
  link: "邀请链接",
  revoke: "撤销邀请链接？",
  remove: "移除成员？",
};

export function WorkspaceMembersPage() {
  const { principal } = useAuth();
  if (!principal) throw new Error("缺少工作空间身份");
  const { workspace, user } = principal;
  const manage = useTeamManager();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState("members");
  const members = useQuery({
    queryKey: ["workspace-members", workspace.id, user.id],
    queryFn: () => listMembers(workspace.id),
    enabled: tab === "members",
    // 受邀者在独立会话兑换。切回成员页签时重新获取，避免显示旧的成员列表。
    staleTime: 0,
    refetchInterval: 30_000,
  });
  const invites = useQuery({
    queryKey: ["workspace-invites", workspace.id, user.id],
    queryFn: () => listInvitations(workspace.id),
    enabled: manage && tab === "links",
    staleTime: 0,
    refetchInterval: 30_000,
  });
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [duration, setDuration] = useState<InviteDuration>(168);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function open(next: Dialog | null) {
    setDialog(next);
    setError("");
  }

  function updateInvites() {
    void queryClient.invalidateQueries({
      queryKey: ["workspace-invites", workspace.id],
    });
  }

  async function perform(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (cause) {
      setError(
        cause instanceof AuthApiError
          ? cause.message
          : "操作未完成，请稍后重试。",
      );
      updateInvites();
    } finally {
      setBusy(false);
    }
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setNotice("已复制邀请链接");
      return true;
    } catch {
      // 剪贴板受浏览器权限或 HTTP 限制时，保留原链接供用户手动复制。
      return false;
    }
  }

  function create() {
    void perform(async () => {
      const { invitation, token } = await createInvitation(
        workspace.id,
        duration,
      );
      open({
        kind: "link",
        record: invitation,
        url: invitationUrl(token),
        created: true,
      });
      updateInvites();
    });
  }

  function copyExisting(record: WorkspaceInvite) {
    void perform(async () => {
      const { token } = await copyInvitation(workspace.id, record.id);
      const url = invitationUrl(token);
      if (!(await copy(url)))
        open({ kind: "link", record, url, created: false });
    });
  }

  function confirm(dialog: Extract<Dialog, { kind: "revoke" | "remove" }>) {
    void perform(async () => {
      if (dialog.kind === "revoke") {
        await revokeInvitation(workspace.id, dialog.record.id);
        updateInvites();
        setNotice("邀请链接已撤销");
      } else {
        await removeMember(workspace.id, dialog.member.id);
        void members.refetch();
        setNotice("成员已移除");
      }
      open(null);
    });
  }

  const memberCount = members.data?.length;
  const showInvites = () => {
    setTab("links");
    open(null);
  };
  return (
    <SettingsPage
      title="成员与邀请"
      description={`管理「${workspace.name}」的成员，以及邀请同事加入的链接。`}
      scope="工作空间级"
      headingAction={
        manage && (
          <Button
            theme="solid"
            className="workspace-member-invite-button"
            icon={<UserPlus size={16} />}
            onClick={() => {
              setDuration(168);
              setNotice("");
              open({ kind: "create" });
            }}
          >
            邀请成员
          </Button>
        )
      }
    >
      {!manage && <Banner description="仅工作空间管理员可邀请或移除成员。" />}
      {!dialog && notice && <Banner type="success" description={notice} />}
      {!dialog && error && <Banner type="danger" description={error} />}
      <Tabs
        activeKey={manage ? tab : "members"}
        onChange={(key) => {
          setNotice("");
          setError("");
          setTab(key);
        }}
        className="workspace-members-tabs"
      >
        <TabPane
          itemKey="members"
          tab={memberCount === undefined ? "成员" : `成员 ${memberCount}`}
        >
          <Card
            title="工作空间成员"
            description="加入后的成员可参与授权范围内的任务协作。"
          >
            <SettingsLoading
              loading={members.isPending}
              error={members.error}
              retry={members.refetch}
            />
            {members.data && (
              <ul className="workspace-members-list">
                {members.data.map((member) => (
                  <li className="workspace-member-row" key={member.id}>
                    <div className="workspace-member-person">
                      <Avatar>{Array.from(member.display_name)[0]}</Avatar>
                      <div>
                        <strong>{member.display_name}</strong>
                        {member.id === user.id && (
                          <small className="workspace-member-you">你</small>
                        )}
                        <small className="workspace-member-email">
                          {member.email}
                        </small>
                      </div>
                    </div>
                    <div className="workspace-member-actions">
                      <Tag color={member.role === "owner" ? "blue" : "grey"}>
                        {roleName(member.role)}
                      </Tag>
                      {manage &&
                        member.role === "member" &&
                        member.id !== user.id && (
                          <Dropdown>
                            <Button
                              theme="borderless"
                              icon={<Ellipsis size={16} />}
                              aria-label={`${member.display_name}的更多操作`}
                            />
                            <Dropdown.Popover placement="bottom end">
                              <Dropdown.Menu
                                aria-label={`${member.display_name}的更多操作`}
                              >
                                <Dropdown.Item
                                  id="remove"
                                  variant="danger"
                                  onAction={() =>
                                    open({ kind: "remove", member })
                                  }
                                >
                                  移除成员
                                </Dropdown.Item>
                              </Dropdown.Menu>
                            </Dropdown.Popover>
                          </Dropdown>
                        )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </TabPane>
        {manage && (
          <TabPane itemKey="links" tab="邀请链接">
            <Card
              title="邀请链接"
              description="有效链接可以复制或撤销。已使用、已撤销及过期的链接保留记录。"
            >
              <SettingsLoading
                loading={invites.isPending}
                error={invites.error}
                retry={invites.refetch}
              />
              {invites.data &&
                (invites.data.length === 0 ? (
                  <Empty
                    title="还没有邀请链接"
                    description="创建链接后，可以把它发给需要加入的同事。"
                  />
                ) : (
                  <div className="workspace-invites-list">
                    <div
                      className="workspace-invite-row workspace-invite-labels"
                      aria-hidden="true"
                    >
                      <span>创建信息</span>
                      <span>状态</span>
                      <span>到期时间</span>
                      <span>操作</span>
                    </div>
                    <ul className="workspace-members-list">
                      {invites.data.map((record) => (
                        <li className="workspace-invite-row" key={record.id}>
                          <div>
                            <strong>
                              邀请链接 ·{" "}
                              {formatTime.format(new Date(record.created_at))}
                            </strong>
                            <small>
                              {record.used_by_name
                                ? `使用人：${record.used_by_name}`
                                : `创建人：${record.creator_name}`}
                            </small>
                          </div>
                          <span>
                            <Tag color={statusTone[record.status]}>
                              {statusLabel[record.status]}
                            </Tag>
                          </span>
                          <small className="workspace-invite-expiry">
                            <span>到期 </span>
                            {formatTime.format(new Date(record.expires_at))}
                          </small>
                          <div className="workspace-member-actions">
                            {record.status === "pending" ? (
                              <>
                                <Button
                                  size="small"
                                  disabled={busy}
                                  icon={<Copy size={14} />}
                                  onClick={() => copyExisting(record)}
                                >
                                  复制
                                </Button>
                                <Button
                                  size="small"
                                  disabled={busy}
                                  theme="borderless"
                                  onClick={() =>
                                    open({ kind: "revoke", record })
                                  }
                                >
                                  撤销
                                </Button>
                              </>
                            ) : (
                              <span className="workspace-invite-no-action">
                                —
                              </span>
                            )}
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
            </Card>
          </TabPane>
        )}
      </Tabs>
      {dialog && (
        <Modal
          title={
            dialog.kind === "link" && dialog.created
              ? "邀请链接已创建"
              : dialogTitles[dialog.kind]
          }
          visible
          width={490}
          maskClosable={!busy}
          closeOnEsc={!busy}
          onCancel={() => {
            if (!busy) open(null);
          }}
        >
          <div className="workspace-invite-dialog">
            {error && <Banner type="danger" description={error} />}
            {dialog.kind === "create" && (
              <>
                <p>创建链接后，把它发给要加入「{workspace.name}」的同事。</p>
                <div className="form-field">
                  <span>有效期</span>
                  <Select
                    aria-label="有效期"
                    value={String(duration)}
                    onChange={(value) => setDuration(value === "24" ? 24 : 168)}
                    optionList={[
                      { value: "168", label: "7 天（默认）" },
                      { value: "24", label: "24 小时" },
                    ]}
                  />
                </div>
                <Banner
                  title="一次性链接 · 普通成员"
                  description="持有链接的人登录后可加入一次，请仅发送给受邀同事。"
                />
                <div className="settings-actions">
                  <Button disabled={busy} onClick={() => open(null)}>
                    取消
                  </Button>
                  <Button theme="solid" loading={busy} onClick={create}>
                    创建链接
                  </Button>
                </div>
              </>
            )}
            {dialog.kind === "link" && (
              <>
                <p>
                  复制后发给同事。链接将在{" "}
                  {formatTime.format(new Date(dialog.record.expires_at))} 失效。
                </p>
                <div className="workspace-invite-copy">
                  <Input
                    aria-label="邀请地址"
                    value={dialog.url}
                    readOnly
                    onFocus={(event) => event.currentTarget.select()}
                  />
                  <Button
                    theme="solid"
                    icon={<Copy size={16} />}
                    onClick={() => {
                      void copy(dialog.url).then((copied) => {
                        setError(
                          copied
                            ? ""
                            : "无法访问剪贴板，请手动选中并复制上方链接。",
                        );
                        if (copied) showInvites();
                      });
                    }}
                  >
                    复制链接
                  </Button>
                </div>
                <Banner
                  title="只能使用一次"
                  description="对方加入后链接即失效。可在「邀请链接」中提前撤销。"
                />
                <div className="settings-actions">
                  <Button onClick={showInvites}>查看邀请链接</Button>
                  <Button theme="borderless" onClick={() => open(null)}>
                    完成
                  </Button>
                </div>
              </>
            )}
            {(dialog.kind === "revoke" || dialog.kind === "remove") && (
              <>
                <p>
                  {dialog.kind === "revoke"
                    ? "撤销后，持有此链接的人将无法加入。已加入的成员保留其访问权限。"
                    : `移除后，${dialog.member.display_name}将失去对「${workspace.name}」及其中任务的访问权限。`}
                </p>
                <div className="settings-actions">
                  <Button disabled={busy} onClick={() => open(null)}>
                    取消
                  </Button>
                  <Button
                    type="danger"
                    theme="solid"
                    loading={busy}
                    onClick={() => confirm(dialog)}
                  >
                    {dialog.kind === "revoke" ? "撤销链接" : "移除成员"}
                  </Button>
                </div>
              </>
            )}
          </div>
        </Modal>
      )}
    </SettingsPage>
  );
}
