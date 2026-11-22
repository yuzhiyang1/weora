"""展开团队采纳模板，只向评分器提供逻辑路径与已召回的文本。"""

import hashlib
import json
from uuid import UUID

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.generated_rubric import fill_template
from weora.modules.knowledge.models import AdoptionRubric, KnowledgeSessionScorer, ScorerLabel
from weora.modules.knowledge.resource_paths import normalize_resource_path

ADOPTION_STANDARD = "teamai-adoption"


def validate_adoption_template(prompt: str, labels: list[ScorerLabel], *, directory: bool) -> None:
    if directory:
        raise KnowledgeError("KNOWLEDGE_ADOPTION_TEMPLATE", "请用单个 teamai-adoption.md 发布团队采纳标准。", 422)
    try:
        fill_template(prompt, labels, {
            "doc.id": "00000000-0000-4000-8000-000000000000",
            "doc.path": 'guides/a "quoted" #1.md', "doc.file": "1234567890ab.md",
        })
    except ValueError as error:
        raise KnowledgeError("KNOWLEDGE_ADOPTION_TEMPLATE", str(error), 422) from error


def adoption_question(entry_id: UUID) -> tuple[str, str]:
    key = hashlib.sha256(str(entry_id).encode()).hexdigest()[:12]
    return f"teamai:adoption:{key}", f"{key}.md"


def render_adoption_rubric(
    standard: KnowledgeSessionScorer, entry_id: UUID, source: dict[str, str],
) -> AdoptionRubric | None:
    name, filename = adoption_question(entry_id)
    path = source.get("resource_path")
    if path is None and source.get("type") in {"file", "wiki"}:
        path = source.get("ref")
    logical = normalize_resource_path(path) if path else None
    try:
        prompt, labels = fill_template(standard.prompt, standard.labels, {
            "doc.id": str(entry_id), "doc.path": logical or f"knowledge/{entry_id}.md", "doc.file": filename,
        })
    except ValueError:
        # 不能因个别文档使模板失效而放宽已发布的隔离要求。
        if standard.isolation == "verified":
            raise KnowledgeError("KNOWLEDGE_ADOPTION_TEMPLATE", "无法安全展开此知识的采纳评分模板。", 422) from None
        return None
    rubric: AdoptionRubric = {
        "id": str(standard.id), "name": name, "revision": standard.revision, "prompt": prompt, "labels": labels,
        "passing_score": standard.passing_score, "model": standard.model, "tool": standard.tool,
        "evidence_path": f"recalled/{filename}", "source_hash": "",
    }
    if standard.isolation is not None:
        rubric["isolation"] = standard.isolation
    rubric["source_hash"] = hashlib.sha256(json.dumps(
        rubric, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return rubric
