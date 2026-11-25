// Weora managed
import { connectSession } from "./weora-collaboration/bridge.mjs";

function channels() {
  const active = new Map<string, ReturnType<typeof connectSession>>();
  return {
    add(id: string, name: string, cwd: string, deliver: (text: string) => Promise<string>) {
      if (active.get(id)?.active) return;
      active.set(id, connectSession({ runtime: "opencode", id, name, cwd, deliver }));
    },
    close(id: string) {
      active.get(id)?.close();
      active.delete(id);
    },
    stop() {
      for (const channel of active.values()) channel.close();
      active.clear();
    },
  };
}

// OpenCode 1.x 在 server hook 提供 SDK，2.x 在 setup 提供原生 session API。
async function server({ client, directory }) {
  const network = channels();
  const childSessions = new Set<string>();
  const last = new Map<string, { agent: string; model: unknown }>();
  return {
    event: async ({ event }) => {
      const details = event.properties;
      const info = details?.info;
      if (event.type === "session.created" && info?.parentID) childSessions.add(info.id);
      if (event.type === "session.deleted" && info?.id) {
        network.close(info.id);
        childSessions.delete(info.id);
        last.delete(info.id);
      }
      if (event.type === "message.updated" && info?.role === "user") {
        last.set(info.sessionID, { agent: info.agent, model: info.model });
      }
      if (!["session.created", "session.updated"].includes(event.type) || !info?.id ||
          info.parentID || childSessions.has(info.id)) return;
      network.add(info.id, info.title || info.id, directory, async (text) => {
        const result = await client.session.promptAsync({
          path: { id: info.id },
          body: { ...last.get(info.id), parts: [{ type: "text", text }] },
        });
        return result.response?.status === 204 ? "accepted" : "unknown";
      });
    },
    dispose: () => network.stop(),
  };
}

export default {
  id: "weora-collaboration",
  server,
  setup(ctx) {
    if (!ctx.event) return;
    const network = channels();
    const childSessions = new Set<string>();
    const abort = new AbortController();
    void (async () => {
      for await (const event of ctx.event.subscribe({ signal: abort.signal })) {
        const details = event.data ?? {};
        const id = details.sessionID;
        if (!id) continue;
        if (event.type === "session.created" && details.parentID) childSessions.add(id);
        if (event.type === "session.deleted") {
          network.close(id);
          childSessions.delete(id);
        }
        if (childSessions.has(id) || !["session.created", "session.execution.succeeded",
          "session.execution.failed", "session.execution.interrupted"].includes(event.type)) continue;
        const info = await ctx.session.get({ sessionID: id });
        if (info.parentID) continue;
        network.add(id, info.title || id, event.location?.directory || ctx.location.directory, async (text) => {
          await ctx.session.prompt({ sessionID: id, text, delivery: "queue" });
          return "accepted";
        });
      }
    })().catch(() => network.stop());
    return () => {
      abort.abort();
      network.stop();
    };
  },
};
