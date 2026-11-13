import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Banner, Button, Card, Empty, Modal, Skeleton } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { decideSkillContribution, getSkillContribution, listSkillContributions, type SkillContribution } from "./api";
import { SkillFileBrowser } from "./SkillFileBrowser";
import { PackageNotes } from "./SkillPackageNotes";
import { formatDate } from "./SkillsPage";

/** 待审包可单独预览，接受后仍需通过现有发布流程。 */
export function SkillContributions({ workspaceId, skillId, archived, hasDraft, onChanged }: {
  workspaceId: string;
  skillId: string;
  archived: boolean;
  hasDraft: boolean;
  onChanged: (accept: boolean) => Promise<void>;
}) {
  const client = useQueryClient();
  const [selected, setSelected] = useState<SkillContribution | null>(null);
  const queryKey = ["skill-contributions", workspaceId, skillId];
  const list = useQuery({ queryKey, queryFn: () => listSkillContributions(workspaceId, skillId) });
  if (list.isPending) return <Skeleton active />;
  if (list.isError) return <AssetLoadError title="待审核列表暂时无法加载" description={list.error.message}
    onRetry={() => void list.refetch()} />;

  return <div className="skill-contributions">
    {!list.data.length && <Empty title="没有待审核的上传" />}
    {list.data.map((item) => <Card key={item.id} className="detail-card" title={item.package_name}
      headerExtraContent={<Button onClick={() => setSelected(item)}>查看候选包</Button>}>
      <div className="detail-inline-meta">
        <span>上传人 {item.uploader_name || "未知"}</span>
        <time dateTime={item.created_at}>{formatDate(item.created_at)}</time>
      </div>
    </Card>)}
    {selected && <ReviewDialog key={selected.id} workspaceId={workspaceId} skillId={skillId} item={selected}
      archived={archived} hasDraft={hasDraft} onClose={() => setSelected(null)}
      onChanged={async (accept) => {
        await client.invalidateQueries({ queryKey });
        setSelected(null);
        await onChanged(accept);
      }} />}
  </div>;
}

/** 确认之前展示候选包自身的文件和校验结果。 */
function ReviewDialog({ workspaceId, skillId, item, archived, hasDraft, onClose, onChanged }: {
  workspaceId: string;
  skillId: string;
  item: SkillContribution;
  archived: boolean;
  hasDraft: boolean;
  onClose: () => void;
  onChanged: (accept: boolean) => Promise<void>;
}) {
  const details = useQuery({
    queryKey: ["skill-contribution", workspaceId, skillId, item.id],
    queryFn: () => getSkillContribution(workspaceId, skillId, item.id),
  });
  const review = useMutation({
    mutationFn: (accept: boolean) => decideSkillContribution(workspaceId, skillId, item.id, accept),
    onSuccess: (_, accept) => onChanged(accept),
  });
  return <Modal visible title="审核 Skill 候选包" width={950}
    onCancel={onClose} maskClosable={!review.isPending} closeOnEsc={!review.isPending}>
    <div className="skill-content-panel">
      <p>{item.package_name} · 上传人 {item.uploader_name || "未知"}</p>
      {details.isPending && <Skeleton active />}
      {details.isError && <AssetLoadError title="候选包暂时无法加载" description={details.error.message}
        onRetry={() => void details.refetch()} />}
      {details.data && <>
        <SkillFileBrowser workspaceId={workspaceId} skillId={skillId}
          source={{ contributionId: item.id }} files={details.data.files} />
        <PackageNotes record={details.data} />
      </>}
      {review.isError && <Banner closeIcon={null} type="danger" description={review.error.message} />}
      {hasDraft && <p>接受此候选包会替换提交时的草稿。发布版本仍需单独确认。</p>}
      {archived && <p>恢复 Skill 后可以接受候选包。</p>}
      <div className="skill-review-actions">
        <Button disabled={review.isPending} onClick={onClose}>关闭</Button>
        <Button disabled={review.isPending} onClick={() => review.mutate(false)}>驳回</Button>
        <Button disabled={!details.data || archived || review.isPending} onClick={() => review.mutate(true)}
          theme="solid">接受为草稿</Button>
      </div>
    </div>
  </Modal>;
}
