"""长正文保留完整可读引用；摘要、分页和已读回执不能成为跨任务读全文的通道。"""

import json

import pytest

from tests.coordination.test_engine import accept, message, setup
from weora.modules.coordination.domain import CoordinationError
from weora.modules.coordination.worker_reads import worker_facts


def test_large_ticket_has_bounded_preview_and_exact_full_text_chunks() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    original = state.triggers[0]
    original.text = "长正文" * 8000
    original.origin["root_text"] = "根消息" * 8000
    for index in range(20):
        engine.receive(message(f"follow-{index}"))
    preview = worker_facts(state, identity, {})
    assert isinstance(preview["inputs"], list)
    assert len(preview["inputs"]) == 8 and preview["next_offset"] == 8
    assert len(json.dumps(preview, ensure_ascii=False)) < 20000
    chunks = [
        worker_facts(state, identity, {"resource": "input", "id": original.id, "offset": offset})["text"]
        for offset in range(0, len(original.text), 4000)
    ]
    assert all(isinstance(chunk, str) for chunk in chunks)
    assert "".join(str(chunk) for chunk in chunks) == original.text
    assert worker_facts(state, identity, {"resource": "root"})["chars"] == 24000
    outside = message("another", actor="other-user")
    outside.threads = ["chat:another-topic"]
    engine.receive(outside)
    with pytest.raises(CoordinationError, match="EVIDENCE_NOT_DELIVERED"):
        worker_facts(state, identity, {"resource": "input", "id": "another"})
