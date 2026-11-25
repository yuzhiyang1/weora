"""从固定 Skill 证据生成 TeamAI 风格的路由评判标准。"""

import hashlib
import json
from uuid import UUID

from weora.modules.knowledge.domain import KnowledgeError, ScoringIsolation
from weora.modules.knowledge.generated_rubric import fill_template
from weora.modules.knowledge.models import (
    DeliveredSkillEvidence,
    KnowledgeSessionScorer,
    RoutingQuestion,
    RoutingRubric,
    ScorerLabel,
    SessionSkillEvidence,
)

ROUTING_STANDARD = "teamai-routing"
ROUTING_NAME_PREFIX = "teamai:routing:"

_BUILTIN_LABELS: list[ScorerLabel] = [
    {"value": "fit", "score": 1, "description": "会话加载了目标 Skill，且它的能力符合本次任务。"},
    {"value": "idle", "score": 1, "description": "会话未加载目标 Skill，任务无需其能力，或已用其他方法完成相关工作。"},
    {"value": "extra", "score": 0, "description": "会话加载了目标 Skill，但本次任务无需其能力。"},
    {"value": "missed", "score": 0,
     "description": "会话需要目标 Skill 的能力，但既未加载，也未以其他方法完成相关工作。"},
]


def routing_name(skill: DeliveredSkillEvidence) -> str:
    return ROUTING_NAME_PREFIX + ":".join(filter(None, (skill["namespace"], skill["name"])))


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_routing_template(prompt: str, labels: list[ScorerLabel], *, directory: bool) -> None:
    if directory:
        raise KnowledgeError("KNOWLEDGE_ROUTING_TEMPLATE", "请用单个 teamai-routing.md 发布团队路由标准。", 422)
    try:
        fill_template(prompt, labels, {"skill.name": "a" * 64})
    except ValueError as error:
        hint = "路由标准只支持 skill.name 插值。" + str(error)
        raise KnowledgeError("KNOWLEDGE_ROUTING_TEMPLATE", hint, 422) from error


def routing_question(
    skill: DeliveredSkillEvidence, skills: SessionSkillEvidence, template: KnowledgeSessionScorer | None,
) -> RoutingQuestion:
    if template is None:
        rubric: RoutingRubric = {
            "id": None, "revision": None, "model": None, "tool": None,
            "prompt": (
                f"评估名为 {skill['name']} 的 Skill 路由。"
                "请使用 evidence.json 中的 skills.available 描述和 skills.used 加载记录。"
            ),
            "labels": _BUILTIN_LABELS, "passing_score": 1.0,
        }
    else:
        try:
            prompt, labels = fill_template(template.prompt, template.labels, {"skill.name": skill["name"]})
        except ValueError as error:
            raise KnowledgeError("KNOWLEDGE_ROUTING_TEMPLATE", "无法为此 Skill 展开团队路由标准。", 422) from error
        rubric = {
            "id": str(template.id), "revision": template.revision, "model": template.model, "tool": template.tool,
            "prompt": prompt, "labels": labels, "passing_score": template.passing_score,
        }
        if template.isolation is not None:
            rubric["isolation"] = template.isolation
    # 描述和安装版本固定在证据摘要里。修改描述后，仍是同一条路由评分标准。
    source = _digest({key: value for key, value in rubric.items() if key not in ("id", "revision")})
    used = skills["used"]
    return {
        "name": routing_name(skill), "source_hash": source,
        "routing": {
            "skill": skill, "used": used, "evidence_hash": _digest([skill, used]), "rubric": rubric,
        },
    }


def routing_label(
    question: RoutingQuestion, label: str, driver: str, operation: UUID, isolation: ScoringIsolation | None = None,
) -> dict[str, object]:
    routing = question["routing"]
    rubric = routing["rubric"]
    score = next((row["score"] for row in rubric["labels"] if row["value"] == label), None)
    if score is None:
        raise KnowledgeError("KNOWLEDGE_ROUTING_LABEL", "标签须来自固定的 Skill 路由标准。", 422)
    result: dict[str, object] = {
        "scorer": question["name"], "skill": {key: value for key, value in routing["skill"].items()
                                            if key != "description"},
        "label": label, "score": score, "passed": score >= rubric["passing_score"], "driver": driver,
        "scorer_id": rubric["id"], "scorer_revision": rubric["revision"], "scorer_source_hash": question["source_hash"],
        "evidence_hash": routing["evidence_hash"], "judge_call_id": str(operation),
    }
    if isolation is not None and driver != "human":
        result["isolation"] = isolation
    return result


def find_routing_question(
    name: str, skills: SessionSkillEvidence | None, template: KnowledgeSessionScorer | None, *, manual: bool = False,
) -> RoutingQuestion:
    if skills is None:
        raise KnowledgeError("KNOWLEDGE_ROUTING_EVIDENCE_MISSING", "此会话版本没有可核验的 Skill 安装快照。", 404)
    skill = next((entry for entry in skills["available"] if routing_name(entry) == name), None)
    if skill is None:
        raise KnowledgeError("KNOWLEDGE_ROUTING_SKILL_NOT_AVAILABLE", "所选 Skill 不在此会话版本的安装快照中。", 404)
    if not manual and skills["used"] is None:
        raise KnowledgeError(
            "KNOWLEDGE_ROUTING_USAGE_UNKNOWN", "此会话的 Skill 加载情况未知，可由成员提交人工标签。", 422,
        )
    return routing_question(skill, skills, template)
