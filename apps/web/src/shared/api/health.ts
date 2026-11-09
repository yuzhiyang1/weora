export type LiveHealth = {
  status: "ok";
  service: "weora-api";
  version: string;
};

export async function getLiveHealth(): Promise<LiveHealth> {
  const response = await fetch("/api/v1/health/live", {
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    throw new Error(`后端健康检查失败：HTTP ${response.status}`);
  }

  const payload: unknown = await response.json();
  if (!isLiveHealth(payload)) {
    throw new Error("后端健康检查返回了无法识别的数据");
  }

  return payload;
}

function isLiveHealth(value: unknown): value is LiveHealth {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  return (
    candidate.status === "ok" &&
    candidate.service === "weora-api" &&
    typeof candidate.version === "string"
  );
}
