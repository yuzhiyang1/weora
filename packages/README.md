# 共享包准入规则

`packages` 用于存放两个或更多客户端真正需要共享的代码。当前只有 Web 客户端，因此暂不创建空的 SDK、通用 UI 或平台适配器。

未来共享代码遵守以下依赖方向：

```text
apps/web ───────┐
apps/desktop ───┼──> packages/*
apps/mobile ────┘
```

- `packages` 不得反向依赖任何 `apps`。
- 数据契约、领域行为和设计令牌不得依赖 DOM、Electron、React Native 等平台运行时。
- Web 和桌面端可以共享 React DOM 实现；移动端不要求复用 Web 页面和 CSS。
- 文件系统、安全存储、通知、网络传输等平台能力保留在各自应用的适配器中。
- 只有出现真实的第二个使用方，或存在必须统一维护的契约时，才建立新的共享包。
