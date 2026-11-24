"""按固定评分标准、运行模式与评审口径汇总工作流标签。"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select

from weora.modules.knowledge.models import KnowledgeWorkflowScore
from weora.modules.knowledge.scoring_report import score_counts, weekly_score_counts
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.knowledge.workflow_score_sync import synced_scores
from weora.modules.knowledge.workflow_scores import DeliverySelection, JudgeIdentity, WorkflowScoreLabel, score_digest
from weora.modules.projects.service import ProjectService

GroupBy = Literal[
    "tool", "model", "team_revision", "definition", "role", "project", "tag", "fingerprint", "skill", "stage",
]
type Sample = tuple[str, WorkflowScoreLabel]
type RubricKey = tuple[str, str, Literal["attended", "unattended"] | None]


@dataclass(frozen=True, slots=True)
class GroupKey:
    kind: Literal["value", "none", "unknown"]
    value: str | None = None
    model_source: str | None = None
    local_changes: bool = False

    @property
    def id(self) -> str:
        return score_digest(self.kind, self.value, self.model_source, self.local_changes)


@dataclass(slots=True)
class Measure:
    rubric: RubricKey
    judge: JudgeIdentity
    samples: list[Sample]

    @property
    def id(self) -> str:
        return score_digest(self.rubric, self.judge)

    @property
    def last_scored_at(self) -> datetime:
        return self.samples[0][1].scored_at

    @property
    def published(self) -> WorkflowScoreLabel | None:
        return next((label for _, label in self.samples if label.rubric.team_revision), None)

    def summary(self) -> dict[str, object]:
        scorer, source_hash, run_mode = self.rubric
        kind, tool, model, model_source = self.judge
        published = self.published
        return {
            "id": self.id, "scorer": scorer, "source_hash": source_hash, "run_mode": run_mode,
            "team_revision": published.rubric.team_revision if published else None,
            "judge": {"kind": kind, "tool": tool, "model": model, "model_source": model_source},
            "stats": _stats(self.samples),
        }


def _stats(samples: list[Sample]) -> dict[str, int | float | None]:
    return score_counts(len(samples), sum(label.passed for _, label in samples))


async def _window(store: KnowledgeStore, since: datetime, now: datetime) -> list[Sample]:
    score = KnowledgeWorkflowScore
    # 评分时间决定入窗。重评分保留审计快照，统计只取同一工作流轮次、标准及评委的最新判定。
    ranked = select(
        score.id, score.payload, score.scored_at,
        func.row_number().over(
            partition_by=score.label_key, order_by=(score.scored_at.desc(), score.id.desc()),
        ).label("position"),
    ).where(
        score.workspace_id == store.scope.workspace, score.project_id == store.scope.project,
        score.scored_at >= since, score.scored_at <= now,
    ).subquery()
    rows = await store.db.execute(select(ranked.c.id, ranked.c.payload).where(
        ranked.c.position == 1,
    ).order_by(ranked.c.scored_at.desc(), ranked.c.id.desc()))
    return [(ident, WorkflowScoreLabel.model_validate(payload)) for ident, payload in rows.tuples()]


def _measures(samples: list[Sample]) -> list[Measure]:
    grouped: dict[RubricKey, dict[JudgeIdentity, list[Sample]]] = defaultdict(lambda: defaultdict(list))
    for item in samples:
        _, label = item
        rubric = label.scorer, label.rubric.source_hash, label.run_mode
        grouped[rubric][label.judge_identity()].append(item)
    measures: list[Measure] = []
    for rubric, judgements in grouped.items():
        models = any(judge[0] == "model" for judge in judgements)
        prior = judgements.get(("stage", None, None, None), [])
        for judge, measured in judgements.items():
            if judge[0] == "stage" and models:
                continue
            # 阶段先行判定不绑定评审模型。它计入同口径的每个模型度量，人工口径独立。
            cohort = measured + prior if judge[0] == "model" else measured
            cohort.sort(key=lambda item: (item[1].scored_at, item[0]), reverse=True)
            measures.append(Measure(rubric, judge, cohort))
    return sorted(measures, key=lambda measure: (measure.rubric[0], measure.id))


def _default(measures: list[Measure]) -> Measure:
    models = [measure for measure in measures if measure.judge[0] == "model"] or measures
    newest = max(models, key=lambda measure: (
        measure.published is not None, (measure.published or measure.samples[0][1]).scored_at,
    ))
    return max(
        (measure for measure in models if measure.rubric[1] == newest.rubric[1]),
        key=lambda measure: (len(measure.samples), measure.last_scored_at),
    )


def _values(values: list[str] | None) -> set[GroupKey]:
    if values is None:
        return {GroupKey("unknown")}
    return {GroupKey("value", value) for value in values} or {GroupKey("none")}


def _one(value: str | None) -> set[GroupKey]:
    return {GroupKey("value", value)} if value else {GroupKey("unknown")}


def _selection(label: WorkflowScoreLabel) -> DeliverySelection | None:
    return label.agent.team.selection if label.agent and label.agent.team else None


def _group_keys(label: WorkflowScoreLabel, by: GroupBy) -> set[GroupKey]:
    agent = label.agent
    selection = _selection(label)
    match by:
        case "stage":
            return _one(label.decided_by)
        case "skill":
            return _values(label.used.skills if label.used else None)
        case "project":
            return _values(selection.projects if selection else None)
        case "tag":
            return _values(selection.subscribed_tags if selection else None)
        case "role":
            role = (selection.step_role or selection.primary_role) if selection else None
            return _values([role] if role else [] if selection else None)
        case "tool":
            return _one(agent.tool.name if agent and agent.tool else None)
        case "model":
            name, source = agent.model.resolved() if agent and agent.model else (None, None)
            return {GroupKey("value", name, source)} if name else _one(None)
        case "team_revision":
            if agent and agent.team:
                return {GroupKey("value", agent.team.revision, local_changes=agent.team.local_changes)}
            return _one(None)
        case "definition":
            return _one(agent.definition.name if agent and agent.definition else None)
        case "fingerprint":
            return _one(agent.fingerprint if agent else None)


def _groups(samples: list[Sample], by: GroupBy) -> dict[GroupKey, list[Sample]]:
    groups: dict[GroupKey, list[Sample]] = defaultdict(list)
    for item in samples:
        # 多值标签分别计数一次，显式空数组与未知信息分别展示。
        for key in _group_keys(item[1], by):
            groups[key].append(item)
    return groups


def _group_rows(groups: dict[GroupKey, list[Sample]], by: GroupBy) -> list[dict[str, object]]:
    kinds = {"value": 0, "none": 1, "unknown": 2}
    ordered = sorted(groups.items(), key=lambda item: (
        kinds[item[0].kind], -len(item[1]), item[0].value or "", item[0].id,
    ))
    rows: list[dict[str, object]] = []
    for key, samples in ordered:
        role_source = None
        if by == "role" and key.kind == "value":
            applied = {bool(selection.step_role) for _, label in samples if (selection := _selection(label))}
            role_source = "both" if len(applied) == 2 else "step" if True in applied else "member"
        rows.append({
            "id": key.id, "kind": key.kind, "value": key.value, "model_source": key.model_source,
            "local_changes": key.local_changes, "role_source": role_source, "stats": _stats(samples),
        })
    return rows


def _failures(samples: list[Sample], limit: int) -> dict[str, object]:
    failed = [(ident, label) for ident, label in samples if not label.passed]
    return {
        "total": len(failed),
        "items": [{
            "id": ident, "label": label.label, "score": label.score, "scored_at": label.scored_at.isoformat(),
            "run_id": label.subject.run_id, "step": label.subject.step, "attempt": label.subject.attempt,
            "decided_by": label.decided_by, "tool": label.agent.tool.name if label.agent and label.agent.tool else None,
        } for ident, label in failed[:limit]],
    }


async def workflow_score_dashboard(
    store: KnowledgeStore, projects: ProjectService, *, days: int, scorer: str | None, cohort: str | None,
    group_by: GroupBy | None, group: str | None, limit: int,
) -> dict[str, object]:
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    samples = await _window(store, since, now)
    samples.extend(await synced_scores(store, projects, since, now))
    # 团队分支和人工导入可含同一标签。最新评分优先，同一时间优先使用团队分支版本。
    current: dict[str, Sample] = {}
    for sample in samples:
        label = sample[1]
        key = label.logical_key()
        kept = current.get(key)
        if kept is None or label.scored_at >= kept[1].scored_at:
            current[key] = sample
    measures = _measures(list(current.values()))
    result: dict[str, object] = {
        "window": {"days": days, "since": since.isoformat(), "until": now.isoformat()},
        "measures": [measure.summary() for measure in measures], "selected": None,
    }
    if not measures:
        return result
    options = [measure for measure in measures if measure.rubric[0] == (scorer or measures[0].rubric[0])]
    if not options:
        options = [measure for measure in measures if measure.rubric[0] == measures[0].rubric[0]]
    selected = next((measure for measure in options if measure.id == cohort), None) or _default(options)
    samples = selected.samples
    groups = _groups(samples, group_by) if group_by else {}
    active = next((key for key in groups if key.id == group), None)
    distribution = Counter((label.label, label.score) for _, label in samples)
    ordered_labels = sorted(distribution.items(), key=lambda item: (-item[0][1], item[0][0]))
    result["selected"] = selected.summary() | {
        "weeks": weekly_score_counts(((label.scored_at, label.passed) for _, label in samples), since, now),
        "labels": [{"value": value, "score": score, "count": count} for (value, score), count in ordered_labels],
        "stages": _group_rows(_groups(samples, "stage"), "stage"),
        "before_judge": sum(label.judge.kind == "stage" for _, label in samples),
        "model_stats": _stats([item for item in samples if item[1].judge.kind == "model"]),
        "groups": _group_rows(groups, group_by) if group_by else [],
        "active_group": active.id if active else None,
        "failures": _failures(groups[active] if active else samples, limit),
    }
    return result
