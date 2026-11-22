import type { ReactNode } from "react";
import { Button, Card, Empty } from "../../components/console";

export type KnowledgeRow = { id: string; title: string; meta: ReactNode; action?: ReactNode; onOpen?: () => void };

export function KnowledgeRows({ title, rows, empty, more }: {
  title: string;
  rows: KnowledgeRow[];
  empty: string;
  more?: ReactNode;
}) {
  return <Card title={title}>
    {rows.length ? <ul className="knowledge-list">
      {rows.map((row) => <li key={row.id} className="knowledge-row">
        <div className="knowledge-row-copy">
          {row.onOpen
            ? <Button size="small" theme="borderless" className="knowledge-row-title" onClick={row.onOpen}>{row.title}</Button>
            : <strong className="knowledge-text">{row.title}</strong>}
          <div className="knowledge-meta">{row.meta}</div>
        </div>
        {row.action}
      </li>)}
    </ul> : <Empty title={empty} />}
    {more && <div className="knowledge-actions">{more}</div>}
  </Card>;
}
