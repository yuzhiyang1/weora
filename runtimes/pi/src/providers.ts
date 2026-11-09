import { createModels, type Api, type Model } from '@earendil-works/pi-ai';
import { deepseekProvider } from '@earendil-works/pi-ai/providers/deepseek';
import { openaiProvider } from '@earendil-works/pi-ai/providers/openai';
import { anthropicProvider } from '@earendil-works/pi-ai/providers/anthropic';
import { googleProvider } from '@earendil-works/pi-ai/providers/google';
import { moonshotaiCnProvider } from '@earendil-works/pi-ai/providers/moonshotai-cn';

export const providerFactories = {
  deepseek: deepseekProvider,
  openai: openaiProvider,
  anthropic: anthropicProvider,
  google: googleProvider,
  'moonshotai-cn': moonshotaiCnProvider,
};
export type SupportedProvider = keyof typeof providerFactories;

export function isSupportedProvider(value: unknown): value is SupportedProvider {
  return typeof value === 'string' && Object.hasOwn(providerFactories, value);
}

/** 只注册平台预设提供方，不载入插件、个人 OAuth 或浏览器指定的地址。 */
export function configuredModels(provider: SupportedProvider) {
  const models = createModels();
  models.setProvider(providerFactories[provider]());
  return models;
}

export function configuredModel(models: ReturnType<typeof createModels>, provider: SupportedProvider, id: string): Model<Api> | undefined {
  const known = models.getModel(provider, id);
  if (known) return known;
  // 锁定 SDK 的新目录已移除旧别名；保留原请求 ID，不能偷偷切换历史运行的模型。
  if (provider === 'deepseek' && ['deepseek-chat', 'deepseek-reasoner'].includes(id)) {
    const template = models.getModels(provider)[0];
    if (template) return { ...template, id, name: id, reasoning: id === 'deepseek-reasoner' };
  }
  return undefined;
}
