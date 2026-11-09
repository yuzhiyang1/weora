import { writeFileSync } from 'node:fs';
import { providerFactories } from './providers.js';

/** SDK 升级后显式生成后端目录；运行时不依赖 Node 子进程或在线模型发现。 */
const providers = Object.entries(providerFactories).map(([id, factory]) => {
  const provider = factory();
  const models = provider.getModels().map(model => ({ id: model.id, name: model.name }));
  if (id === 'deepseek') {
    for (const [alias, name] of [['deepseek-chat', 'DeepSeek Chat'], ['deepseek-reasoner', 'DeepSeek Reasoner']] as const) {
      if (!models.some(model => model.id === alias)) models.push({ id: alias, name: `${name}（兼容既有配置）` });
    }
  }
  return { id, label: id === 'google' ? 'Google Gemini' : id === 'moonshotai-cn' ? 'Moonshot / Kimi（中国区）' : provider.name, models };
});
writeFileSync(new URL('../../../backend/src/weora/modules/console_settings/providers.json', import.meta.url),
  JSON.stringify({ sdk: '@earendil-works/pi-ai@1.3.0', providers }, null, 2) + '\n');
