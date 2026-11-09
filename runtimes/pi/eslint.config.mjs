import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';

// 执行面使用 Node 环境；禁止随意 console 输出，避免污染协议或记录后台凭据。
export default tseslint.config(
  { ignores: ['dist/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { languageOptions: { globals: globals.node }, rules: { 'no-console': 'error' } },
);
