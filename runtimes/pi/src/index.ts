import { serve } from './protocol.js';
import { runConfiguredModel } from './runtime.js';

// 仅输出自有协议，不记录请求、凭据或 SDK 错误栈。
await serve(runConfiguredModel);
