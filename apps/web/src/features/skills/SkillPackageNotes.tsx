import { Banner, Card, Tag } from "../../components/console";
import type { SkillDraft, SkillVersion } from "./api";

/** 包校验和兼容性随选中的包切换，历史版本与候选包也能使用。 */
export function PackageNotes({ record }: { record: SkillDraft | SkillVersion }) {
  return (
    <Card className="detail-card">
      <details className="skill-package-notes" open={record.warnings.length > 0 || undefined}>
        <summary>{record.warnings.length
          ? `校验与兼容性（${record.warnings.length} 项提醒）`
          : "包结构与安全校验已通过"}</summary>
        <div className="skill-content-panel">
          {record.interface?.default_prompt && <div className="checksum-block">
            <h3>默认提示词</h3>
            <div>{record.interface.default_prompt}</div>
          </div>}
          {record.warnings.map((warning) => <Banner key={`${warning.code}-${warning.path}`}
            closeIcon={null} title={warning.blocks_publish ? "阻断发布" : "需要确认"}
            type={warning.blocks_publish ? "danger" : "warning"}
            description={`${warning.message}${warning.path ? ` · ${warning.path}` : ""}`} />)}
          <div className="compatibility-list">
            {record.compatibility.map((item) => (
              <div key={item.target}>
                <Tag color={item.status === "compatible" ? "green" : "orange"}>
                  {compatibilityLabel(item.target)}
                </Tag>
                <small>{item.message}</small>
              </div>
            ))}
          </div>
          <div className="checksum-block">
            <h3>内容校验和（SHA256）</h3>
            <code>{record.content_sha256}</code>
          </div>
        </div>
      </details>
    </Card>
  );
}

function compatibilityLabel(target: string): string {
  return (
    (
      {
        generic_agent_skill: "通用 Agent Skill",
        codex: "Codex",
        claude_code: "Claude Code",
        dify_agent: "Dify Agent",
      } as Record<string, string>
    )[target] ?? target
  );
}
