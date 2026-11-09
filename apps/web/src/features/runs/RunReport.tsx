import { Card } from "@heroui/react";
import { FileText } from "lucide-react";
import { useState } from "react";

import { Button, Empty, Typography } from "../../components/console";
import { MarkdownContent } from "../../components/MarkdownContent";

/** 预览与原文使用同一份持久化输出，不重新总结或修改 Agent 的事实。 */
export function RunReport({ text, title = "执行报告", onViewArtifacts }: {
  text: string | null;
  title?: string;
  onViewArtifacts?: () => void;
}) {
  const [source, setSource] = useState(false);
  return <Card className="run-report" aria-label={title}>
    <Card.Header className="run-report-heading">
      <div className="run-panel-title">
        <FileText size={20} aria-hidden="true" />
        <Typography.Title heading={3}>{title}</Typography.Title>
      </div>
      {text && <Button size="small" theme="light" aria-pressed={source}
        onClick={() => setSource(!source)}>{source ? "返回预览" : "查看原文"}</Button>}
    </Card.Header>
    <Card.Content>
      {text ? source ? <pre className="run-report-source" aria-label="报告原文" tabIndex={0}>{text}</pre>
        : <MarkdownContent>{text}</MarkdownContent>
        : <Empty title="尚无执行报告" description="当前运行没有可展示的文本输出，可在对应页签查看工具事实与产物。" />}
    </Card.Content>
    {onViewArtifacts && <Card.Footer>
      <Button theme="light" onClick={onViewArtifacts}>查看截图与产物</Button>
    </Card.Footer>}
  </Card>;
}
