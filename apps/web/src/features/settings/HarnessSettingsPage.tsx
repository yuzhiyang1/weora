import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, Select, Tag } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { getHarnessSettings, saveHarnessSettings, type HarnessId } from "./api";
import { SettingsLoading, SettingsPage, SettingsResult, useTeamManager } from "./SettingsPage";

export function HarnessSettingsPage() {
  const { principal } = useAuth();
  const workspace = principal!.workspace.id;
  const manager = useTeamManager();
  return (
    <SettingsPage title="Agent 执行内核" description="选择新运行使用的 Worker 执行内核。" scope="团队级">
      {manager ? <HarnessForm key={workspace} workspace={workspace} /> : (
        <Card><p>仅团队所有者和管理员可以管理执行内核。</p></Card>
      )}
    </SettingsPage>
  );
}

function HarnessForm({ workspace }: { workspace: string }) {
  const cache = useQueryClient();
  const queryKey = ["harness-settings", workspace];
  const query = useQuery({ queryKey, queryFn: () => getHarnessSettings(workspace) });
  // 冻结编辑时的版本，后台刷新不会让旧草稿覆盖他人的配置。
  const [draft, setDraft] = useState<{ harness: HarnessId; revision: number }>();
  const mutation = useMutation({
    mutationFn: ({ harness, revision }: { harness: HarnessId; revision: number }) =>
      saveHarnessSettings(workspace, { harness, revision }),
    onSuccess: (saved) => {
      cache.setQueryData(queryKey, saved);
      setDraft(undefined);
    },
  });
  const config = query.data;
  if (!config || query.isError) {
    return <SettingsLoading loading={query.isPending} error={query.error} retry={query.refetch} />;
  }
  const selected = draft?.harness ?? config.harness;
  return (
    <div className="settings-stack">
      <SettingsResult
        error={mutation.error}
        success={mutation.isSuccess ? "已保存。新运行将使用所选内核。" : null}
      />
      <Card title="默认 Worker 内核" headerExtraContent={<Tag>{config.harness === "pi" ? "Pi" : "Oh My Pi"}</Tag>}>
        <form
          className="settings-stack settings-fields"
          onSubmit={(event) => {
            event.preventDefault();
            if (draft && selected !== config.harness && !mutation.isPending) {
              mutation.mutate(draft);
            }
          }}
        >
          <Select
            aria-label="默认 Worker 内核"
            value={selected}
            disabled={mutation.isPending}
            optionList={[
              { label: "Pi", value: "pi" },
              { label: config.omp_available ? "Oh My Pi" : "Oh My Pi（未部署）", value: "oh-my-pi", disabled: !config.omp_available },
            ]}
            onChange={(value) => {
              if (value === "pi" || value === "oh-my-pi") {
                mutation.reset();
                setDraft(value === config.harness ? undefined : { harness: value, revision: config.revision });
              }
            }}
          />
          <p className="form-help">Oh My Pi 使用「模型与密钥」中的主要 DeepSeek 连接。</p>
          <div className="settings-inline harness-settings-actions">
            <p className="form-help">仅影响新 Worker 运行。已有会话沿用原内核。</p>
            <Button htmlType="submit" theme="solid" loading={mutation.isPending} disabled={selected === config.harness || mutation.isPending}>
              保存执行内核
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
