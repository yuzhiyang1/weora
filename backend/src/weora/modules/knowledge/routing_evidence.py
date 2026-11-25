"""按设备回执把会话看到的 Skill 代次转换为可评分的固定证据。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import DeliveredSkillEvidence, SessionSkillEvidence
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.skills.infrastructure.distribution import DeviceActor, captured_installations, utc

MAX_ROUTING_SKILLS = 256


@dataclass(frozen=True, slots=True)
class DeliveryObservation:
    generation: UUID
    observed_at: datetime
    description: str | None


@dataclass(frozen=True, slots=True)
class DeliveryCapture:
    installations: list[DeliveryObservation]
    usage_profile: str | None = None
    usage_session: str | None = None

    def fingerprint(self) -> str:
        # 使用确认时的输入。晚到的安装回执或使用事件不应改变重放收据。
        installations = sorted(
            (str(row.generation), utc(row.observed_at).isoformat(), row.description) for row in self.installations
        )
        encoded = json.dumps([installations, self.usage_profile, self.usage_session], ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()


async def freeze_deliveries(
    store: KnowledgeStore, runtime: str, captured_at: datetime | None, capture: DeliveryCapture | None,
) -> SessionSkillEvidence | None:
    if capture is None:
        return None
    scope = store.scope
    if scope.device_id is None:
        raise KnowledgeError("KNOWLEDGE_SKILL_EVIDENCE_DEVICE", "会话 Skill 证据需要使用配对设备登记。", 403)
    if captured_at is None:
        raise KnowledgeError("KNOWLEDGE_SKILL_EVIDENCE_TIME", "会话 Skill 证据缺少采集时间。", 422)
    boundary = min(utc(captured_at), datetime.now(UTC))
    observed = {row.generation: utc(row.observed_at) for row in capture.installations}
    if any(at > boundary for at in observed.values()):
        raise KnowledgeError("KNOWLEDGE_SKILL_EVIDENCE_TIME", "Skill 安装观察晚于会话证据。", 422)
    actor = DeviceActor(scope.workspace, scope.device_id, scope.user, "", scope.project)
    usage = (capture.usage_profile, capture.usage_session) if capture.usage_profile and capture.usage_session else None
    proofs, loaded = await captured_installations(
        store.db, actor, "claude" if runtime == "claude-code" else runtime, observed,
        usage=usage, captured_at=boundary,
    )
    available: dict[str, DeliveredSkillEvidence] = {}
    used: set[str] = set()
    for row in sorted(capture.installations, key=lambda item: (utc(item.observed_at), item.generation)):
        found = proofs.get(row.generation)
        if found is None:
            # 尚未确认或超出当前范围的安装代次不能成为题目证据。
            continue
        available[found.slug] = {
            "name": found.slug, "namespace": found.namespace,
            "version_id": str(found.version_id), "installation_id": str(row.generation), "description": row.description,
        }
        if found.version_id in loaded:
            used.add(found.slug)
    return {"available": [available[key] for key in sorted(available)],
            "used": sorted(used) if capture.usage_session is not None else None}
