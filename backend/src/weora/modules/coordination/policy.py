"""按上游 policy.ts / core/model.ts 翻译；授权始终由平台依据受信身份复核。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import cast

from weora.modules.coordination.domain import Config, CoordinationError, Json, Mandate, Strength, Trigger


def policy_version(config: Config) -> str:
    # 与上游一致：模型、并发和 pause 不重写接单授权；模式、规则、来源及准入变化会失效旧评估。
    values = {
        key: value
        for key, value in asdict(config).items()
        if key
        not in {
            "revision",
            "paused",
            "max_workers",
            "eval_timeout_seconds",
            "debounce_ms",
            "main_model_id",
            "worker_model_id",
        }
    }
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Gate:
    admitted: bool
    strength: Strength = "inferred"
    reason: str = "admission_ignored"
    basis: Json | None = None


def gate(config: Config, trigger: Trigger, *, participating: bool = False) -> Gate:
    if not config.enabled or not trigger.authorized or trigger.filtered:
        return Gate(False, reason=trigger.filtered or "source_or_actor_revoked")
    if config.source_kind == "feishu" and trigger.origin.get("route_key") and (
        trigger.origin.get("route_key") != config.route_key
        or trigger.origin.get("project_grant_id") != config.project_grant_id
    ):
        return Gate(False, reason="route_grant_changed")
    listed = trigger.actor_open_id in config.allowed_actors
    if not trigger.human:
        allowed = trigger.actor_open_id in config.allowed_bots
        return Gate(
            allowed and config.mode != "off" and (trigger.addressed or config.observe_unmentioned),
            basis={"via": "bot", "mode": config.mode},
        )
    if listed and trigger.addressed:
        return Gate(True, "explicit", basis={"via": "addressed"})
    cell = "listed_unaddressed" if listed else "unlisted_addressed" if trigger.addressed else "unlisted_unaddressed"
    admission = getattr(config, cell) if trigger.addressed or config.observe_unmentioned else "ignore"
    basis: Json = {"via": "admission", "cell": cell, "mode": config.mode}
    if admission == "explicit":
        return Gate(True, "explicit", basis=basis)
    if participating and (listed or config.mode != "off"):
        return Gate(True, "explicit" if listed else "inferred", basis={"via": "followup"})
    if admission == "inferred" and config.mode != "off":
        return Gate(True, "inferred", basis=basis)
    return Gate(False, reason="not_a_member" if not listed else "admission_ignored")


def revalidate(config: Config, mandate: Mandate, *, authorized: bool) -> bool:
    if not authorized or not config.enabled:
        return False
    if mandate.route_key is not None and (
        mandate.route_key != config.route_key
        or mandate.project_id != config.project_id
        or mandate.repository_ids != config.repository_ids
        or mandate.project_grant_id != config.project_grant_id
    ):
        return False
    via = mandate.basis.get("via")
    listed = mandate.actor_open_id in config.allowed_actors
    if via == "bot" and mandate.actor_open_id not in config.allowed_bots:
        return False
    if via == "addressed" and not listed:
        return False
    if via == "admission":
        cell = str(mandate.basis.get("cell", ""))
        if cell == "listed_unaddressed" and not listed:
            return False
        newly_listed_addressed = cell == "unlisted_addressed" and listed
        if cell == "unlisted_unaddressed" and listed:
            cell = "listed_unaddressed"
        rank = {"ignore": 0, "inferred": 1, "explicit": 2}
        if not newly_listed_addressed and rank.get(getattr(config, cell, "ignore"), 0) < rank[mandate.strength]:
            return False
    if mandate.strength == "inferred":
        enabled = {rule.id for rule in config.rules if rule.enabled}
        return config.mode == "auto" and bool(mandate.rules) and set(mandate.rules) <= enabled
    return listed or via == "admission"


def assessment_outcome(config: Config, result: Json, strength: Strength) -> tuple[str, str, Json, tuple[str, ...]]:
    """完整规则覆盖、方向三事实和证据检查；自然语言回答不能代替该结构化决策。"""
    enabled = {rule.id for rule in config.rules if rule.enabled}
    rows = result.get("rule_assessments")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CoordinationError("RULE_ASSESSMENTS_REQUIRED")
    assessments = cast(list[Json], rows)
    ids = [row.get("rule_id") for row in assessments]
    if len(ids) != len(set(map(str, ids))) or set(map(str, ids)) != enabled:
        raise CoordinationError("RULE_ASSESSMENTS_MISMATCH")
    if any(not isinstance(row.get("matched"), bool) or not str(row.get("reason", "")).strip() for row in assessments):
        raise CoordinationError("RULE_ASSESSMENTS_INVALID")
    matched = tuple(str(row["rule_id"]) for row in assessments if row["matched"] is True)
    direction = result.get("direction")
    if not isinstance(direction, dict) or set(direction) != {
        "implementation_requested",
        "target_basis",
        "requires_design_decision",
        "evidence",
    }:
        raise CoordinationError("DIRECTION_REQUIRED")
    if (
        not isinstance(direction.get("implementation_requested"), bool)
        or not isinstance(
            direction.get("requires_design_decision"),
            bool,
        )
        or direction.get("target_basis") not in {"specified", "existing-standard", "undetermined"}
    ):
        raise CoordinationError("DIRECTION_INVALID")
    if not isinstance(direction.get("evidence"), str) or not direction["evidence"].strip():
        raise CoordinationError("DIRECTION_EVIDENCE_REQUIRED")
    decision = result.get("decision")
    reason = result.get("reason")
    if decision not in {"accept", "ignore", "unavailable"} or not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("DECISION_INVALID")
    audit: Json = {"rule_assessments": assessments, "direction": direction, "matched_rules": list(matched)}
    if decision != "accept":
        return str(decision), reason, audit, matched
    if (
        not direction["implementation_requested"]
        or direction["target_basis"] == "undetermined"
        or direction["requires_design_decision"]
    ):
        return "ignore", "directionUnsettled", audit, matched
    if not matched:
        return "ignore", "noRuleMatched", audit, matched
    evidence = result.get("intent_evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise CoordinationError("INTENT_EVIDENCE_REQUIRED")
    audit["intent_evidence"] = evidence
    if strength == "inferred" and config.mode != "auto":
        audit.update(would_accept=True, proposal={key: result.get(key) for key in ("title", "scope", "worktree_name")})
        return "ignore", "observed", audit, matched
    return "accept", reason, audit, matched
