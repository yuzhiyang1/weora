import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, Input, Select, Table } from "../../components/console";
import type { TableColumn } from "../../components/console/data-table";
import { AssetLoadError } from "../../components/console/asset-controls";
import { listMembers } from "../auth/workspace-api";
import { consoleRequest, errorMessage } from "../runs/api";
import type { SkillVersion } from "./api";

type UsageCounts = {
  uses: number;
  mentions: number;
  loads: number;
  users: number;
};
type UsageRow = UsageCounts & { key: string };
type UsageSummary = { totals: UsageCounts; groups: UsageRow[]; total_groups: number };
type Group = "day" | "user" | "version";

function dateField(date: Date) {
  return [date.getFullYear(), date.getMonth() + 1, date.getDate()]
    .map((value) => String(value).padStart(2, "0")).join("-");
}

export function SkillUsagePanel({ workspaceId, skillId, versions, canWrite }: {
  workspaceId: string;
  skillId: string;
  versions: SkillVersion[];
  canWrite: boolean;
}) {
  const [from, setFrom] = useState(() => {
    const date = new Date();
    date.setDate(date.getDate() - 6);
    return dateField(date);
  });
  const [to, setTo] = useState(() => dateField(new Date()));
  const [group, setGroup] = useState<Group>("day");
  const [version, setVersion] = useState("");
  const [user, setUser] = useState("");
  const [page, setPage] = useState(0);
  const start = new Date(from + "T00:00:00");
  const end = new Date(to + "T00:00:00");
  end.setDate(end.getDate() + 1);
  const valid = Number.isFinite(start.getTime()) && Number.isFinite(end.getTime()) && start < end;
  const members = useQuery({
    queryKey: ["workspace-members", workspaceId],
    queryFn: () => listMembers(workspaceId),
    enabled: canWrite,
  });
  const usage = useQuery({
    queryKey: ["skill-usage", workspaceId, skillId, from, to, group, version, user, page],
    enabled: valid,
    queryFn: () => {
      const query = new URLSearchParams({
        from: start.toISOString(), to: end.toISOString(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        group_by: group, offset: String(page * 50), limit: "50",
      });
      if (version) query.set("version_id", version);
      if (user) query.set("user_id", user);
      return consoleRequest<UsageSummary>(
        workspaceId, "/skills/" + encodeURIComponent(skillId) + "/usage?" + query,
      );
    },
  });
  const label = (key: string) => {
    if (group === "version") return versions.find((entry) => entry.id === key)?.version ?? key;
    if (group === "user") return members.data?.find((entry) => entry.id === key)?.display_name ?? key;
    return key;
  };
  const columns: TableColumn<UsageRow>[] = [
    { title: { day: "日期", user: "成员", version: "版本" }[group], dataIndex: "key", render: label },
    { title: "使用次数", dataIndex: "uses" },
    { title: "点名次数", dataIndex: "mentions" },
    { title: "确认加载", dataIndex: "loads" },
    { title: "使用人数", dataIndex: "users" },
  ];
  return (
    <Card title="Skill 使用统计">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <label>开始日期<Input aria-label="开始日期" type="date" value={from}
          onChange={(value) => { setFrom(value); setPage(0); }} /></label>
        <label>结束日期<Input aria-label="结束日期" type="date" value={to}
          onChange={(value) => { setTo(value); setPage(0); }} /></label>
        <div><span id="skill-usage-group">分组</span><Select aria-labelledby="skill-usage-group" value={group}
          optionList={[
            { value: "day", label: "按日期" }, { value: "version", label: "按版本" },
            ...(canWrite ? [{ value: "user", label: "按成员" }] : []),
          ]}
          onChange={(value) => {
            if (value === "day" || value === "version" || value === "user") { setGroup(value); setPage(0); }
          }} /></div>
        <div><span id="skill-usage-version">版本</span><Select aria-labelledby="skill-usage-version" value={version}
          optionList={[{ value: "", label: "全部版本" }, ...versions.map((v) => ({ value: v.id, label: v.version }))]}
          onChange={(value) => { setVersion(String(value)); setPage(0); }} /></div>
        {canWrite && <div><span id="skill-usage-member">成员</span><Select aria-labelledby="skill-usage-member"
          value={user} loading={members.isPending}
          optionList={[{ value: "", label: "全部成员" },
            ...(members.data ?? []).map((m) => ({ value: m.id, label: m.display_name }))]}
          onChange={(value) => { setUser(String(value)); setPage(0); }} /></div>}
      </div>
      <div className="mt-4">
        {!valid && <p>请选择有效的日期范围。</p>}
        {valid && usage.isPending && <p role="status">正在读取使用统计…</p>}
        {usage.isError && <AssetLoadError title="无法读取使用统计" description={errorMessage(usage.error)}
          onRetry={() => void usage.refetch()} />}
        {usage.data && <>
          <p className="mb-3">使用 {usage.data.totals.uses} 次 · {usage.data.totals.users} 人 ·
            点名 {usage.data.totals.mentions} 次 · 确认加载 {usage.data.totals.loads} 次</p>
          <p className="mb-3 text-sm text-muted">统计来自已开启采集的设备，同一轮的点名和加载合计为一次使用。</p>
          {usage.data.groups.length > 0
            ? <Table columns={columns} dataSource={usage.data.groups} rowKey="key" />
            : <p>所选范围暂无使用记录。</p>}
          {usage.data.total_groups > 50 && <div className="mt-3 flex flex-wrap gap-2">
            <Button disabled={!page} onClick={() => setPage(page - 1)}>上一页</Button>
            <Button disabled={(page + 1) * 50 >= usage.data.total_groups}
              onClick={() => setPage(page + 1)}>下一页</Button>
          </div>}
        </>}
      </div>
    </Card>
  );
}
