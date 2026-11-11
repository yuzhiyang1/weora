import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { runKey, type RunDetail, type RunStatus } from "./api";

type RunState = {
  status: RunStatus;
  last_sequence: number;
  title: string | null;
};

const retryDelays = [1000, 2000, 4000, 8000, 16000];

export function useRunWatch(
  workspaceId: string,
  userId: string,
  runId: string,
  enabled: boolean,
  onRevoked: () => void,
) {
  const client = useQueryClient();
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  const [interrupted, setInterrupted] = useState(false);
  const [exhausted, setExhausted] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    function visibilityChanged() {
      const showing = document.visibilityState !== "hidden";
      // 从后台返回时，收到通过授权的快照后才重新展示正文。
      if (!showing && typeof EventSource !== "undefined") setInterrupted(true);
      setVisible(showing);
      if (showing) setExhausted(false);
    }
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => document.removeEventListener("visibilitychange", visibilityChanged);
  }, []);

  useEffect(() => {
    if (!enabled || !visible || typeof EventSource === "undefined") return;
    const key = runKey(workspaceId, userId, runId);
    const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(runId)}/watch`;
    let active = true;
    let failures = 0;
    let source: EventSource | undefined;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let refresh: ReturnType<typeof setTimeout> | undefined;
    let accessTimeout: ReturnType<typeof setTimeout> | undefined;
    let latest: RunState | undefined;
    let refreshing = false;

    function queueRefresh() {
      const current = client.getQueryData<RunDetail>(key);
      if (!latest || !current || refreshing || refresh !== undefined) return;
      if (
        current.status === latest.status &&
        current.last_sequence >= latest.last_sequence &&
        (!latest.title || latest.title === current.task_title)
      ) return;
      refresh = setTimeout(async () => {
        refresh = undefined;
        refreshing = true;
        const target = latest;
        await Promise.all([
          client.invalidateQueries({ queryKey: key, exact: true }),
          client.invalidateQueries({ queryKey: ["run-conversation", workspaceId, userId, runId] }),
          client.invalidateQueries({ queryKey: ["run-tools", workspaceId, userId, runId] }),
          client.invalidateQueries({ queryKey: ["run-delivery", workspaceId, userId, runId] }),
        ]);
        refreshing = false;
        // 请求过程中若收到更新，合并后追一次；请求失败由页面的刷新入口处理。
        if (active && latest !== target) queueRefresh();
      }, 250);
    }

    function connect() {
      if (!active) return;
      const connection = new EventSource(path, { withCredentials: true });
      source = connection;
      function close() {
        if (!active || source !== connection) return false;
        clearTimeout(accessTimeout);
        connection.close();
        source = undefined;
        return true;
      }
      function interruptedConnection() {
        if (!close()) return;
        clearTimeout(refresh);
        refresh = undefined;
        setInterrupted(true);
        const delay = retryDelays[failures++];
        if (delay === undefined) setExhausted(true);
        else reconnect = setTimeout(connect, delay + Math.random() * 250);
      }
      function expectAccessCheck() {
        clearTimeout(accessTimeout);
        // 代理或连接卡死时，即使浏览器未报错也要停止显示旧的私有正文。
        accessTimeout = setTimeout(interruptedConnection, 12_000);
      }
      expectAccessCheck();
      connection.addEventListener("run_state", (event) => {
        if (!active || source !== connection || !(event instanceof MessageEvent)) return;
        try {
          const snapshot = JSON.parse(event.data) as RunState;
          if (!Number.isSafeInteger(snapshot.last_sequence) || snapshot.last_sequence < 0) return;
          latest = snapshot;
          expectAccessCheck();
          setInterrupted(false);
          queueRefresh();
        } catch {
          // 异常数据不覆盖已确认的状态，等待服务端下一份快照。
        }
      });
      connection.addEventListener("access_confirmed", () => {
        if (active && source === connection) {
          failures = 0;
          expectAccessCheck();
        }
      });
      connection.addEventListener("stream_rotate", () => {
        if (close()) {
          failures = 0;
          reconnect = setTimeout(connect, 100 + Math.random() * 250);
        }
      });
      connection.addEventListener("access_revoked", () => {
        if (close()) {
          clearTimeout(refresh);
          onRevoked();
        }
      });
      connection.onerror = interruptedConnection;
    }

    connect();
    return () => {
      active = false;
      source?.close();
      clearTimeout(reconnect);
      clearTimeout(refresh);
      clearTimeout(accessTimeout);
    };
  }, [workspaceId, userId, runId, client, enabled, visible, attempt, onRevoked]);

  return {
    visible,
    interrupted,
    exhausted,
    retry() {
      setExhausted(false);
      setAttempt((value) => value + 1);
    },
  };
}
