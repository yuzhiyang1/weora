import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Banner, Button, Card, Input, Modal, Select, Tag } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { consoleRequest, consoleWrite, errorMessage } from "../runs/api";
import { CopyInstruction, SessionStatus } from "../tasks/AgentCollaborationPanel";
import {
  createPairing, listContactMembers, listDevices, listMySessions, revokeDevice, saveContactMembers,
} from "../tasks/agent-collaboration-api";
import { SettingsLoading, SettingsPage, SettingsResult } from "./SettingsPage";

export function LocalAgentsPage() {
  const { principal } = useAuth();
  return principal ? <LocalAgentsSettings key={principal.workspace.id} workspace={principal.workspace.id} /> : null;
}

function LocalAgentsSettings({ workspace }: { workspace: string }) {
  const cache = useQueryClient();
  const scope = ["local-agents", workspace];
  const [pairing, setPairing] = useState(false);
  const [name, setName] = useState("");
  const [edit, setEdit] = useState<{ id: string; name: string; members: string[] }>();
  const [revoking, setRevoking] = useState<{ id: string; name: string }>();
  const [skillDevice, setSkillDevice] = useState<{ id: string; name: string }>();
  const [feedback, setFeedback] = useState("");
  const pairingFinish = useRef<HTMLButtonElement>(null);
  const devices = useQuery({ queryKey: [...scope, "devices"], queryFn: () => listDevices(workspace), refetchInterval: 8000 });
  const grants = useQuery({
    queryKey: [...scope, "skill-grants"],
    queryFn: () => consoleRequest<{ items: { device_id: string; usage_enabled: boolean }[] }>(
      workspace, "/skill-distribution/grants",
    ),
  });
  const sessions = useQuery({ queryKey: [...scope, "sessions"], queryFn: () => listMySessions(workspace), refetchInterval: 8000 });
  const members = useQuery({ queryKey: [...scope, "members"], queryFn: () => listContactMembers(workspace), enabled: !!edit });
  const refresh = () => void cache.invalidateQueries({ queryKey: scope });
  const pair = useMutation({ mutationFn: () => createPairing(workspace, name.trim()), onSuccess: refresh });
  useEffect(() => {
    // 提交表单被一次性配对码替换时，将焦点留在对话框内。
    if (pairing && pair.data) pairingFinish.current?.focus();
  }, [pairing, pair.data]);
  const remove = useMutation({
    mutationFn: (id: string) => revokeDevice(workspace, id),
    onSuccess: () => { setRevoking(undefined); setFeedback("设备已撤销。"); refresh(); },
  });
  const contacts = useMutation({
    mutationFn: (value: NonNullable<typeof edit>) => saveContactMembers(workspace, value.id, value.members),
    onSuccess: () => { setEdit(undefined); setFeedback("直接联系权限已更新。"); refresh(); },
  });
  const changeSkillGrant = useMutation({
    mutationFn: ({ id, usage }: { id: string; usage: boolean | null }) =>
      consoleWrite(workspace, `/skill-distribution/devices/${encodeURIComponent(id)}/grant`,
        usage === null ? undefined : { usage_enabled: usage }, usage === null ? "DELETE" : "PUT"),
    onSuccess: () => { setSkillDevice(undefined); setFeedback("设备的 Skill 权限已更新。"); refresh(); },
  });
  const selectedGrant = grants.data?.items.find((item) => item.device_id === skillDevice?.id);
  const cancelPairing = () => {
    if (pair.isPending) return;
    setPairing(false);
    setName("");
    pair.reset();
  };
  return (
    <SettingsPage
      title="本地 Agent"
      description="配对开发设备，管理同事可以直接联系的本地会话。"
      scope="个人设置"
      headingAction={<Button type="primary" theme="solid" onClick={() => setPairing(true)}>配对设备</Button>}
    >
      <SettingsResult success={feedback} />
      <Card title="已登记设备">
        <div className="agent-collab-stack">
          <SettingsLoading loading={devices.isPending} error={devices.error} retry={devices.refetch} />
          <SettingsLoading loading={grants.isPending} error={grants.error} retry={grants.refetch} />
          {devices.data?.items.length === 0 && <p className="task-muted">暂无设备。先配对运行本地 Agent 的电脑。</p>}
          {devices.data?.items.map((device) => {
            const inactive = Boolean(device.revoked_at) || device.expired;
            const grant = grants.data?.items.find((item) => item.device_id === device.id);
            return (
              <div className="agent-collab-row" key={device.id}>
                <strong>{device.name}</strong>
                <div className="agent-collab-actions">
                  <Tag>{device.revoked_at ? "已撤销" : inactive ? "已过期" : device.paired ? "已配对" : "等待配对"}</Tag>
                  {!inactive && device.paired && grants.data && <>
                    {grant && <Tag>{grant.usage_enabled ? "同步并统计" : "仅同步"}</Tag>}
                    <Button size="small" onClick={() => {
                      changeSkillGrant.reset();
                      setSkillDevice(device);
                    }}>Skill 权限</Button>
                  </>}
                  {!inactive && <Button size="small" onClick={() => { remove.reset(); setRevoking(device); }}>撤销</Button>}
                </div>
              </div>
            );
          })}
        </div>
      </Card>
      <Card title="我的原生会话">
        <div className="agent-collab-stack">
          <SettingsLoading loading={sessions.isPending} error={sessions.error} retry={sessions.refetch} />
          {sessions.data?.items.length === 0 && (
            <>
              <p className="task-muted">安装对应客户端的 hook，然后打开或恢复会话。</p>
              <code className="agent-collab-command">weora collab install-hook --runtime codex</code>
              <code className="agent-collab-command">weora collab install-hook --runtime claude-code</code>
              <p className="task-muted">可追加 <code>--auto</code> 启用原生唤醒，其他客户端可用 <code>weora collab register</code> 接入。</p>
            </>
          )}
          {sessions.data?.items.map((session) => (
            <div className="agent-collab-row" key={session.id}>
              <div>
                <strong>{session.name}</strong>
                <p className="task-muted">{[session.runtime, session.device, session.repo, session.branch].filter(Boolean).join(" · ")}</p>
                {session.allowed_members.length > 0 && <p className="task-muted">已允许 {session.allowed_members.length} 位成员直接联系</p>}
              </div>
              <div className="agent-collab-actions">
                <SessionStatus session={session} />
                <Button size="small" onClick={() => {
                  contacts.reset();
                  setEdit({ id: session.id, name: session.name, members: session.allowed_members });
                }}>直接联系权限</Button>
              </div>
            </div>
          ))}
        </div>
      </Card>
      {pairing && (
        <Modal title="配对本地设备" visible onCancel={cancelPairing} maskClosable={!pair.isPending} closeOnEsc={!pair.isPending}>
          <div className="agent-collab-stack">
            {pair.data ? (
              <>
                <p>在开发电脑的终端运行以下命令，再输入一次性配对码。</p>
                <CopyInstruction text={`weora collab pair --url ${window.location.origin} --workspace ${workspace}`} label="复制命令" />
                <CopyInstruction text={pair.data.pairing_code} label="复制配对码" />
                <p className="task-muted">配对码有效期 10 分钟。配对成功后运行 <code>weora collab connect</code> 保持在线。</p>
                <div className="agent-collab-actions">
                  <Button ref={pairingFinish} onClick={cancelPairing}>完成</Button>
                </div>
              </>
            ) : (
              <form className="agent-collab-stack" onSubmit={(event) => { event.preventDefault(); if (name.trim()) pair.mutate(); }}>
                <label htmlFor="local-device-name">设备名称</label>
                <Input id="local-device-name" value={name} maxLength={80} onChange={setName} />
                {pair.isError && <Banner type="danger" description={errorMessage(pair.error)} />}
                <div className="agent-collab-actions">
                  <Button htmlType="submit" type="primary" loading={pair.isPending} disabled={!name.trim()}>生成配对码</Button>
                </div>
              </form>
            )}
          </div>
        </Modal>
      )}
      {edit && (
        <Modal title={`允许直接联系 · ${edit.name}`} visible
          onCancel={() => { if (!contacts.isPending) setEdit(undefined); }}
          maskClosable={!contacts.isPending} closeOnEsc={!contacts.isPending}>
          <div className="agent-collab-stack">
            <p>选中的成员可以找到这条会话并向它发消息。清空成员后保存即可撤销权限。</p>
            <SettingsLoading loading={members.isPending} error={members.error} retry={members.refetch} />
            {members.data && (
              <>
                <Select aria-label="可直接联系的成员" multiple value={edit.members}
                  optionList={members.data.items.map((person) => ({ value: person.id, label: person.name }))}
                  onChange={(value) => { if (Array.isArray(value)) setEdit({ ...edit, members: value.map(String) }); }}
                  disabled={contacts.isPending} />
                <div className="agent-collab-actions">
                  <Button type="primary" loading={contacts.isPending}
                    onClick={() => contacts.mutate({ ...edit, members: edit.members.filter((id) =>
                      members.data.items.some((person) => person.id === id)) })}>保存</Button>
                </div>
              </>
            )}
            <SettingsResult error={contacts.error} />
          </div>
        </Modal>
      )}
      {revoking && (
        <Modal title={`撤销设备 · ${revoking.name}`} visible
          onCancel={() => { if (!remove.isPending) setRevoking(undefined); }}
          maskClosable={!remove.isPending} closeOnEsc={!remove.isPending}>
          <div className="agent-collab-stack">
            <p>撤销后，这台设备的会话会从可联系目录消失，也无法继续收发消息。</p>
            <SettingsResult error={remove.error} />
            <div className="agent-collab-actions">
              <Button type="danger" loading={remove.isPending} onClick={() => remove.mutate(revoking.id)}>确认撤销</Button>
            </div>
          </div>
        </Modal>
      )}
      {skillDevice && (
        <Modal title={`Skill 权限 · ${skillDevice.name}`} visible
          onCancel={() => { if (!changeSkillGrant.isPending) setSkillDevice(undefined); }}
          maskClosable={!changeSkillGrant.isPending} closeOnEsc={!changeSkillGrant.isPending}>
          <div className="agent-collab-stack">
            <p>授权后，这台设备可以安装和同步工作空间已发布的 Skill。</p>
            <p>开启统计后，设备会报告使用的 Skill 版本、时间和点名或加载信号。提示词、工具输出和本机路径留在设备上。</p>
            <SettingsResult error={changeSkillGrant.error} />
            <div className="agent-collab-actions">
              {selectedGrant && <Button disabled={changeSkillGrant.isPending}
                onClick={() => changeSkillGrant.mutate({ id: skillDevice.id, usage: null })}>关闭同步</Button>}
              <Button disabled={changeSkillGrant.isPending}
                onClick={() => changeSkillGrant.mutate({ id: skillDevice.id, usage: false })}>仅同步</Button>
              <Button type="primary" loading={changeSkillGrant.isPending}
                onClick={() => changeSkillGrant.mutate({ id: skillDevice.id, usage: true })}>同步并统计</Button>
            </div>
          </div>
        </Modal>
      )}
    </SettingsPage>
  );
}
