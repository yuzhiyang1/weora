import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card } from "@heroui/react";
import { ChevronDown, ChevronUp, Monitor } from "lucide-react";
import { useEffect, useState, type MouseEvent } from "react";

import { Banner, Button, Empty, Spin, Tag, Typography } from "../../components/console";
import { consoleRequest, consoleWrite, errorMessage } from "./api";
import { ComputerInputQueue } from "./computer-input";
import "./computer.css";

type Status = {
  state: "disabled" | "stopped" | "running";
  holder?: "bot" | "human";
  transitioning?: boolean;
  control_id?: string | null;
};
type Screen = { base64: string; width: number; height: number };
type Input = { x: number; y: number; button: 1 | 3; clicks: 1 | 2 } | { key: string };
type Action = "start" | "stop" | "take" | "release";
const keys: Record<string, string> = {
  Enter: "Return", Backspace: "BackSpace", Delete: "Delete", " ": "space",
  ArrowLeft: "Left", ArrowRight: "Right", ArrowUp: "Up", ArrowDown: "Down",
  Home: "Home", End: "End", PageUp: "Page_Up", PageDown: "Page_Down",
};

export function ComputerPanel({ workspaceId, runId, instruction }: {
  workspaceId: string; runId: string; instruction?: string;
}) {
  const path = `/runs/${encodeURIComponent(runId)}/computer`;
  const [inputQueue] = useState(() => new ComputerInputQueue());
  const queryClient = useQueryClient();
  const queryKey = ["run-computer", workspaceId, runId] as const;
  const [inputError, setInputError] = useState<string | null>(null);
  const [showInstruction, setShowInstruction] = useState(false);
  const status = useQuery({
    queryKey,
    queryFn: ({ signal }) => consoleRequest<Status>(workspaceId, path, { signal, cache: "no-store" }),
    refetchInterval: (query) => query.state.data?.state === "disabled" ? false : 2000,
    retry: false,
    gcTime: 0,
  });
  const running = status.data?.state === "running" && !status.isError;
  const screen = useQuery({
    queryKey: [...queryKey, "screen"],
    queryFn: ({ signal }) => consoleRequest<Screen>(workspaceId, `${path}/screen`, { signal, cache: "no-store" }),
    enabled: running,
    refetchInterval: 1000,
    retry: false,
    gcTime: 0,
  });
  const change = useMutation({
    mutationFn: (action: Action) => {
      inputQueue.cancel();
      return consoleWrite(workspaceId, `${path}/${action}`, {});
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
  });
  const human = running && status.data?.holder === "human" && !status.data.transitioning;
  const busy = change.isPending || status.data?.transitioning;
  const frame = screen.isError ? undefined : screen.data;
  useEffect(() => {
    // 接管令牌改变、失去控制或离开页面时，旧队列均不得继续发送。
    inputQueue.cancel();
    return () => inputQueue.cancel();
  }, [inputQueue, workspaceId, runId, human, busy, status.data?.control_id]);

  async function send(input: Input) {
    const controlId = status.data?.control_id;
    if (!human || busy || !controlId) return;
    setInputError(null);
    await inputQueue.enqueue(
      () => consoleWrite(workspaceId, `${path}/input`, { ...input, control_id: controlId }),
      (error) => {
        setInputError(errorMessage(error));
        void status.refetch();
      },
    );
  }

  function point(event: MouseEvent<HTMLButtonElement>, button: 1 | 3) {
    if (!frame || !human || busy || (button === 1 && event.detail === 0)) return;
    event.preventDefault();
    event.currentTarget.focus();
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = Math.max(0, Math.min(frame.width - 1, Math.floor((event.clientX - bounds.left) * frame.width / bounds.width)));
    const y = Math.max(0, Math.min(frame.height - 1, Math.floor((event.clientY - bounds.top) * frame.height / bounds.height)));
    // 浏览器双击已经触发两次 click，每次只发送一次，避免累计成三击。
    void send({ x, y, button, clicks: 1 });
  }

  if (status.isPending) return <Card className="run-computer"><Spin tip="正在读取虚拟电脑状态" /></Card>;
  if (status.isError) return <Card className="run-computer">
    <Banner type="danger" title="暂时无法连接虚拟电脑" description={errorMessage(status.error)} closeIcon={null} />
    <Button onClick={() => void status.refetch()}>重试连接</Button>
  </Card>;
  if (status.data.state === "disabled") {
    return <Card className="run-computer"><Empty title="当前运行未启用虚拟电脑" /></Card>;
  }

  return (
    <Card className="run-computer" role="region" aria-label="虚拟电脑">
      <Card.Header className="run-computer-toolbar">
        <div className="run-panel-title">
          <Monitor size={20} aria-hidden="true" />
          <Typography.Title heading={3}>虚拟电脑</Typography.Title>
          <Tag color={status.data.transitioning ? "orange" : human ? "blue" : running ? "green" : "grey"}>
            {status.data.transitioning ? "正在切换控制权" : human ? "你正在控制桌面" : running ? "Agent 控制中" : "电脑已停止"}
          </Tag>
        </div>
        <div className="run-computer-actions">
          {running ? (
            <>
              <Button type="primary" theme="solid" disabled={busy} onClick={() => change.mutate(human ? "release" : "take")}>
                {human ? "交还控制权" : "获取控制权"}
              </Button>
              <Button theme="light" disabled={busy} onClick={() => change.mutate("stop")}>停止电脑</Button>
            </>
          ) : (
            <Button type="primary" theme="solid" loading={change.isPending} disabled={busy} onClick={() => change.mutate("start")}>
              启动电脑
            </Button>
          )}
        </div>
      </Card.Header>
      <Card.Content className="run-computer-body">
      {change.isError && <Banner type="danger" description={errorMessage(change.error)} closeIcon={null} />}
      {inputError && <Banner type="danger" description={inputError} closeIcon={null} />}
      {running && screen.isError && <Banner type="danger" description={errorMessage(screen.error)} closeIcon={null} />}
      {running && !frame && !screen.isError && <Spin />}
      {!running && <Empty title="桌面尚未启动" description="启动电脑后，即可查看桌面或接管操作。" />}
      {running && frame && (
        <button
          type="button"
          className="run-computer-screen"
          aria-label={human ? "远程 Linux 桌面，点击后可输入按键" : "远程 Linux 桌面，仅查看"}
          disabled={!human || busy}
          onClick={(event) => point(event, 1)}
          onContextMenu={(event) => point(event, 3)}
          onKeyDown={(event) => {
            // Tab 和 Escape 留给控制台焦点导航。
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            const key = keys[event.key] ?? (/^[\x21-\x7e]$/.test(event.key) ? event.key : undefined);
            if (!key || !human || busy) return;
            event.preventDefault();
            void send({ key });
          }}
        >
          <img src={`data:image/jpeg;base64,${frame.base64}`} alt="虚拟电脑当前画面"
            width={frame.width} height={frame.height} draggable={false} />
        </button>
      )}
      {running && frame && <div className="run-computer-caption">
        <span>画面每秒更新 · {frame.width} × {frame.height}</span>
        <span>{human ? "点击桌面后可输入。按 Tab 移回控制台。" : "接管后可操作，交还后由 Agent 继续。"}</span>
      </div>}
      </Card.Content>
      {instruction && <Card.Footer className="run-computer-instruction">
        <Button block theme="light" aria-expanded={showInstruction} onClick={() => setShowInstruction(!showInstruction)}>
          <span className="run-instruction-heading"><span>任务说明</span><span>{showInstruction ? "收起原文" : "展开原文"}</span></span>
          {showInstruction ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}
        </Button>
        {showInstruction && <pre className="run-instruction-source" aria-label="任务指令原文" tabIndex={0}>{instruction}</pre>}
      </Card.Footer>}
    </Card>
  );
}
