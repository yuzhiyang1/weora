import { Avatar, Switch, Label, Separator } from "@heroui/react";
import { useRef, useState, type FormEvent } from "react";
import { useOutletContext } from "react-router-dom";
import { Button, Card, Input } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { useSettingsDirty } from "./SettingsEnvironment";
import { SettingsPage, SettingsResult } from "./SettingsPage";
import { AccountWorkspaces } from "./AccountWorkspaces";

/** 上传前重新编码 PNG，去除原始元数据并限制持久化大小。 */
async function prepareAvatar(file: File): Promise<string> {
  if (
    !["image/png", "image/jpeg"].includes(file.type) ||
    file.size > 5 * 1024 * 1024
  )
    throw new Error("请选择 5MB 以内的 PNG 或 JPG 图片");
  const bitmap = await createImageBitmap(file);
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 256;
    canvas.height = 256;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("浏览器无法处理图片");
    const edge = Math.min(bitmap.width, bitmap.height);
    context.drawImage(
      bitmap,
      (bitmap.width - edge) / 2,
      (bitmap.height - edge) / 2,
      edge,
      edge,
      0,
      0,
      256,
      256,
    );
    const encoded = canvas.toDataURL("image/png");
    if (encoded.length > 340000)
      throw new Error("图片内容过大，请选择更简单的头像");
    return encoded;
  } finally {
    bitmap.close();
  }
}
export function ProfileSettingsPage() {
  const { principal, updateProfile } = useAuth();
  if (!principal) throw new Error("缺少登录身份");
  const [name, setName] = useState(principal.user.display_name);
  const [avatar, setAvatar] = useState<string | null | undefined>();
  const [busy, setBusy] = useState(false);
  const [imageBusy, setImageBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const { dark, setDark } = useOutletContext<{
    dark: boolean;
    setDark: (value: boolean) => void;
  }>();
  const dirty = name !== principal.user.display_name || avatar !== undefined;
  useSettingsDirty(dirty || imageBusy);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || imageBusy) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const saved = await updateProfile(name.trim(), avatar);
      setName(saved.user.display_name);
      setAvatar(undefined);
      setSuccess("个人资料已保存");
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  const image = avatar === undefined ? principal.user.avatar_url : avatar;
  return (
    <SettingsPage
      title="个人资料"
      description="管理你的账户资料与个人外观偏好。"
      scope="个人级"
    >
      <form className="settings-stack" onSubmit={submit}>
        <Card title="头像与账户">
          <div className="settings-stack">
            <div className="settings-avatar-row">
              <Avatar size="lg" color="accent" className="avatar-square">
                {image && <Avatar.Image src={image} alt="个人头像" />}
                <Avatar.Fallback>
                  {Array.from(principal.user.display_name)[0]}
                </Avatar.Fallback>
              </Avatar>
              <div>
                <div className="settings-inline">
                  <Button
                    disabled={busy || imageBusy}
                    loading={imageBusy}
                    onClick={() => input.current?.click()}
                  >
                    更换头像
                  </Button>
                  {image && (
                    <Button
                      theme="borderless"
                      disabled={busy || imageBusy}
                      onClick={() => setAvatar(null)}
                    >
                      移除
                    </Button>
                  )}
                </div>
                <p className="form-help">
                  PNG / JPG，最大 5MB，建议使用正方形图片。
                </p>
              </div>
              <input
                ref={input}
                type="file"
                accept="image/png,image/jpeg"
                hidden
                onChange={async (event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (!file) return;
                  setImageBusy(true);
                  setError(null);
                  try {
                    setAvatar(await prepareAvatar(file));
                  } catch (cause) {
                    setError(cause);
                  } finally {
                    setImageBusy(false);
                  }
                }}
              />
            </div>
            <div className="settings-fields settings-stack">
              <label className="form-field">
                <span>显示名称</span>
                <Input
                  required
                  maxLength={80}
                  value={name}
                  disabled={busy}
                  onChange={setName}
                />
              </label>
              <label className="form-field">
                <span>邮箱</span>
                <Input type="email" value={principal.user.email} readOnly />
                <small className="form-help">
                  当前登录邮箱。邮件验证服务接入后开放变更。
                </small>
              </label>
            </div>
          </div>
          <Separator className="settings-divider" />
          <section className="settings-appearance" aria-label="外观">
            <h2>外观</h2>
            <div className="settings-row">
              <div>
                <strong>深色模式</strong>
                <p className="form-help">外观立即生效，并保存在当前浏览器。</p>
              </div>
              <Switch
                isSelected={dark}
                onChange={setDark}
                aria-label="深色模式"
              >
                <Switch.Content>
                  <Switch.Control>
                    <Switch.Thumb />
                  </Switch.Control>
                </Switch.Content>
                <Label className="sr-only">深色模式</Label>
              </Switch>
            </div>
          </section>
          <SettingsResult error={error} success={success} />
          <div className="settings-actions settings-actions-start">
            <Button
              theme="solid"
              htmlType="submit"
              loading={busy}
              disabled={!dirty || imageBusy || !name.trim()}
            >
              保存修改
            </Button>
          </div>
        </Card>
      </form>
      <AccountWorkspaces />
    </SettingsPage>
  );
}
