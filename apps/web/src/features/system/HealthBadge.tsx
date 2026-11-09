import { useQuery } from "@tanstack/react-query";
import { Tag } from "../../components/console";

import { getLiveHealth } from "../../shared/api/health";

export function HealthBadge() {
  const healthQuery = useQuery({
    queryKey: ["system", "live-health"],
    queryFn: getLiveHealth,
  });

  if (healthQuery.isPending) {
    return (
      <Tag className="health-tag" color="grey">
        正在连接
      </Tag>
    );
  }

  if (healthQuery.isError) {
    return (
      <span title={healthQuery.error.message}>
        <Tag className="health-tag" color="red">
          后端未连接
        </Tag>
      </span>
    );
  }

  return (
    <span title={`v${healthQuery.data.version}`}>
      <Tag className="health-tag" color="green">
        后端已连接
      </Tag>
    </span>
  );
}
