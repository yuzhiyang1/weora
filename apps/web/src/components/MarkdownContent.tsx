import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import "./markdown.css";

/** 模型正文按 Markdown 展示；原始 HTML 作为文字，禁止远程图片自动发起请求。 */
function safeUrl(url: string) {
  const safe = defaultUrlTransform(url);
  if (/^[\s\\]|^\/\//.test(safe)) return "";
  if (/^[a-z][a-z\d+.-]*:/i.test(safe) && !/^(https?:|mailto:)/i.test(safe)) return "";
  return safe;
}

const components: Components = {
  // 报告位于页面标题之下，避免模型中的一级标题打乱控制台的语义层级。
  h1: ({ children }) => <h4>{children}</h4>,
  h2: ({ children }) => <h4>{children}</h4>,
  h3: ({ children }) => <h5>{children}</h5>,
  table: ({ children }) => (
    <div className="markdown-table-scroll" role="region" aria-label="报告表格" tabIndex={0}>
      <table>{children}</table>
    </div>
  ),
  a: ({ href, children, title }) => (
    <a href={href || undefined} title={title}
      target={href && /^(https?:|mailto:)/i.test(href) ? "_blank" : undefined}
      rel="noopener noreferrer">{children}</a>
  ),
  // 图片通过平台已有的产物预览鉴权，不信任模型生成的外链或 data URL。
  img: ({ alt }) => <span className="markdown-image-note">图片{alt ? `：${alt}` : ""}（请在产物中查看）</span>,
};

export function MarkdownContent({ children }: { children: string }) {
  return <div className="markdown-content">
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={safeUrl}>
      {children}
    </ReactMarkdown>
  </div>;
}
