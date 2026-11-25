"""绑定 Registry 事务与知识库的发布、路径校验及归档回调。"""

from weora.modules.knowledge.skill_releases import record_registry_release, record_registry_status
from weora.modules.knowledge.target_reservations import guard_registry_publication, lock_knowledge_targets
from weora.modules.skills.infrastructure.unit_of_work import RegistryPublicationHooks

KNOWLEDGE_SKILL_PUBLICATION = RegistryPublicationHooks(
    lock_knowledge_targets, guard_registry_publication, record_registry_release, record_registry_status,
)
