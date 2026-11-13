# ADR 0029 Weora CLI 浏览器登录

- 状态：Accepted
- 日期：2026-11-12
- 上游参考：[飞书 CLI 设备授权源码](https://github.com/larksuite/cli/blob/05e9c6ef74290f78601892ed9eb8a6389d8fc8ca/internal/auth/device_flow.go)
- 页面设计：[Figma 登录授权页](https://www.figma.com/design/EWNggQy85AQhotQm3EaKk9?node-id=2-16)

Weora CLI 复用控制台账号和本地 Agent 设备身份。CLI 创建一个 10 分钟有效的请求，浏览器使用当前账号和工作空间确认，CLI 再用系统钥匙串中的验证串兑换设备凭据。用户可在终端和页面核对同一验证码。

浏览器审批使用现有会话和 CSRF 校验，并核对页面显示的账号与工作空间。服务端保存验证串与设备凭据的摘要，相同请求的兑换重试返回同一设备。同一站点重新登录时撤销旧设备，切换到其他站点时清除本机旧凭据。退出时撤销当前设备，设备到期后重新执行登录。

Skill 同步和使用统计只对当前 CLI 配置对应的设备生效。切换设备后通过 `weora skill bind` 更新本机绑定。旧的手工配对仍可使用。非交互客户端可通过 `--no-wait` 返回浏览器地址，然后在原机器用 `--resume` 兑换。

Compose 中的 API 只信任固定地址的 Nginx 转发客户端 IP。若默认网段与宿主机网络冲突，用 `WEORA_PROXY_SUBNET` 指定子网，用 `WEORA_PROXY_IP_RANGE` 指定其中的动态地址池，并将 `WEORA_PROXY_IP` 设置为同一子网内、动态池外的地址。

飞书 CLI 会按业务 scope 请求 OAuth access token 与 refresh token。Weora 当前使用 30 天有效的可撤销设备凭据，`auth status` 直接向服务端验证设备身份。
