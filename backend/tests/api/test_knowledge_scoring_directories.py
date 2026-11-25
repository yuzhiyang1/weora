"""评分目录配置覆盖根字段，并保护已领取的评分。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeScoringAttempt, KnowledgeScoringPolicy

pytestmark = pytest.mark.anyio


async def test_scoring_directories_merge_and_recheck_in_flight_attempts(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    settings = f"{root}/settings/session-scoring"
    delivery = f"{settings}/namespaces/Delivery"
    risk = f"{settings}/namespaces/risk"
    visible = {"project_id": str((await project(runs, "交付项目"))["id"])}
    unrelated = {"project_id": str((await project(runs, "其他项目"))["id"])}
    auth = runs.headers()

    async def save(url: str, payload: dict[str, object]) -> dict[str, object]:
        result = await runs.client.put(url, headers=auth, json=payload)
        assert result.status_code == 200, result.text
        data: dict[str, object] = result.json()
        return data

    async def claim(scope: dict[str, str], operation: UUID | None = None) -> dict[str, object]:
        result = await runs.client.post(f"{root}/session-scoring/claim", params=scope, headers=auth, json={
            "operation_id": str(operation or uuid4()), "run_mode": "attended",
        })
        assert result.status_code == 200, result.text
        data: dict[str, object] = result.json()
        return data

    for mode in ("attended", "unattended"):
        await save(f"{settings}/{mode}", {"action": "ask", "daily_limit": 12, "batch": True})
    await save(f"{root}/settings/resource-namespaces", {
        "base_revision": 0,
        "roles": {"risk": {"scorers": ["risk"]}}, "members": {runs.user_id: "risk"},
        "projects": {visible["project_id"]: {"scorers": ["Delivery"]}},
    })

    async with member(runs, "目录普通成员") as (teammate, _):
        response = await teammate.get(delivery)
        assert response.status_code == 403
        response = await teammate.put(delivery, headers=headers(teammate), json={"base_revision": 0})
        assert response.status_code == 403
    response = await runs.client.put(delivery, headers=auth, params=visible, json={"base_revision": 0})
    assert response.status_code == 422

    first_config = {"base_revision": 0, "sessions": {"attended": "auto"}, "batch": False, "daily_limit": 2}
    first = await save(delivery, first_config)
    assert await save(delivery, first_config) == first
    outdated = await runs.client.put(delivery, headers=auth, json=first_config | {"batch": True})
    assert outdated.status_code == 409 and outdated.json()["detail"]["code"] == "KNOWLEDGE_SCORING_POLICY_VERSION"
    assert (await runs.client.get(delivery)).json() == first
    root_mode = {"action": "ask", "daily_limit": 12, "batch": True, "source": "workspace"}
    assert (await runs.client.get(settings, params=unrelated)).json()["attended"] == root_mode
    views = (await runs.client.get(settings, params=visible)).json()
    assert views == {
        "attended": {"action": "auto", "daily_limit": 2, "source": "namespace"},
        "unattended": {"action": "ask", "daily_limit": 2, "source": "namespace"},
    }

    scorer = await runs.client.put(f"{root}/session-scorers/quality", params=visible, headers=auth, json={
        "prompt": "Check project evidence.", "namespace": "Delivery", "passing_score": 1, "sessions": True,
        "sampling_rate": 100, "labels": [
            {"value": "pass", "score": 1, "description": "通过"}, {"value": "fail", "score": 0, "description": "退回"},
        ],
    })
    assert scorer.status_code == 200, scorer.text
    archive = await runs.client.post(f"{root}/shared-sessions", params=visible, headers=auth, json={
        "session": str(uuid4()), "runtime": "codex", "title": "交付证据",
        "turns": [{"role": "assistant", "text": "Finished."}],
    })
    assert archive.status_code == 201, archive.text
    operation = uuid4()
    opened = await claim(visible, operation)
    assert opened["status"] == "open"
    assert await claim(visible, operation) == opened
    async with runs.sessions() as db:
        attempt = await db.get(KnowledgeScoringAttempt, operation)
        assert attempt is not None
        pinned = await db.get(KnowledgeScoringPolicy, attempt.policy_id)
        assert pinned is not None
        assert (pinned.namespace, pinned.run_mode, pinned.action) == ("Delivery", "attended", "auto")

    conflicting = await save(risk, {"base_revision": 0, "sessions": {"attended": "ask"}, "batch": True})
    conflict = {"field": "batch", "namespaces": ["Delivery", "risk"]}
    assert await claim(visible, operation) == {"status": "off", "conflict": conflict}
    result = await runs.client.post(f"{root}/session-scorers/plan", params=visible, headers=auth,
                                    json={"scorers": ["quality"]})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "KNOWLEDGE_SCORING_SETTINGS_CONFLICT"
    result = await runs.client.post(f"{root}/session-scoring/attempts/{operation}/settle", params=visible, headers=auth,
                                    json={"label": "pass"})
    assert result.json() == {"status": "failed", "reason": "policy_revoked"}

    reconciled = await save(risk, {
        "base_revision": conflicting["revision"], "sessions": {"attended": "ask"}, "daily_limit": 1, "batch": False,
    })
    assert (await runs.client.get(settings, params=visible)).json()["attended"] == {
        "action": "ask", "daily_limit": 1, "source": "namespace",
    }
    assert await claim(visible) == {"status": "ask"}
    await save(risk, {"base_revision": reconciled["revision"], "sessions": True, "daily_limit": 0})
    assert await claim(visible) == {"status": "off"}
    workspace = await runs.client.get(settings, params={"workspace_defaults": 1, **visible})
    assert workspace.json()["attended"] == root_mode


async def test_directory_recall_adoption_only_controls_automatic_questions(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    settings = f"{root}/settings/session-scoring"
    delivery = f"{settings}/namespaces/delivery"
    risk = f"{settings}/namespaces/risk"
    scope = {"project_id": str((await project(runs, "自动采纳项目"))["id"])}
    auth = runs.headers()

    async def put(path: str, payload: dict[str, object]) -> dict[str, object]:
        result = await runs.client.put(path, headers=auth, json=payload)
        assert result.status_code == 200, result.text
        data: dict[str, object] = result.json()
        return data

    async def post(path: str, **payload: object) -> httpx.Response:
        return await runs.client.post(root + path, params=scope, headers=auth, json=payload)

    async def claim(operation: str) -> dict[str, object]:
        result = await post("/session-scoring/claim", operation_id=operation, run_mode="attended", accept_adoption=True)
        assert result.status_code == 200, result.text
        data: dict[str, object] = result.json()
        return data

    await put(settings + "/attended", {"action": "auto"})
    await put(root + "/settings/resource-namespaces", {
        "base_revision": 0, "roles": {"risk": {"scorers": ["risk"]}}, "members": {runs.user_id: "risk"},
        "projects": {scope["project_id"]: {"scorers": ["delivery"]}},
    })
    entry = await post("", operation_id=str(uuid4()), title="electrum telescope", body="Apply the original procedure.")
    assert entry.status_code == 201, entry.text

    async def capture() -> str:
        session = str(uuid4())
        recalled = await post("/recall", query="electrum telescope", session=session, recall_mode=True)
        assert recalled.status_code == 200, recalled.text
        saved = await post("/shared-sessions", session=session, runtime="codex", title="Captured",
                           captured_at=datetime.now(UTC).isoformat(),
                           turns=[{"role": "assistant", "text": "I used the original procedure."}])
        assert saved.status_code == 201, saved.text
        return session

    session = await capture()
    assert await claim(str(uuid4())) == {"status": "empty"}
    await put(delivery, {"base_revision": 0, "recall_adoption": True})
    attempt = str(uuid4())
    assert (await claim(attempt))["status"] == "open"
    manual = await post("/adoption/candidates", session=session)
    assert manual.status_code == 403 and manual.json()["detail"]["code"] == "KNOWLEDGE_ADOPTION_DISABLED"
    outcome = await post(f"/adoption/attempts/{attempt}/settle", label="adopted")
    assert outcome.status_code == 200 and outcome.json()["adoption"]["applied"] is True, outcome.text

    second = await capture()
    attempt = str(uuid4())
    assert (await claim(attempt))["status"] == "open"
    restriction = await put(risk, {"base_revision": 0, "recall_adoption": False})
    await put(root + "/settings/session-context/recall-adoption", {"enabled": True})
    effective = (await runs.client.get(settings, params=scope)).json()["attended"]
    assert effective["action"] == "auto" and effective["recall_adoption"] is False
    assert await claim(attempt) == {"status": "failed", "attempt_id": attempt}
    revoked = await post(f"/adoption/attempts/{attempt}/settle", label="adopted")
    assert revoked.json() == {"status": "failed", "reason": "policy_revoked"}
    assert await claim(str(uuid4())) == {"status": "empty"}
    manual = await post("/adoption/candidates", session=second)
    assert manual.status_code == 200 and manual.json()["items"][0]["id"] == entry.json()["id"]
    await put(risk, {"base_revision": restriction["revision"]})
    effective = (await runs.client.get(settings, params=scope)).json()["attended"]
    assert effective["recall_adoption"] is True
