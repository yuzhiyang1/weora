"""覆盖 Swift 协议边和配置声明从索引到召回的生命周期。"""

from uuid import UUID, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import KnowledgeCodebase
from weora_code_graph.extractor import EXTRACTOR_VERSION, extract, resolved_implementations

pytestmark = pytest.mark.anyio


def test_swift_conformances_follow_target_visibility_and_unique_declarations() -> None:
    file = "Packages/A/Sources/Payments/Adapters/Receipt.swift"
    protocol = "Packages/A/Sources/Payments/Protocols.swift"
    files = {
        file: r'''
@MainActor public final class PaymentAdapter<Item: Other>: LocalPort,
    ReceiptPort, Shadowed, PrivatePort, Imported.NotLocal where Item: Protocol {}

extension Container.Receipt: ReceiptPort {}
fileprivate protocol LocalPort {}
let quoted = #"struct Fake: ReceiptPort {}"#
// struct Commented: ReceiptPort {}
/* nested /* protocol Shadow {} */ struct Hidden: ReceiptPort {} */
let note = """
protocol ReceiptPort {}
"""
let formatted = "\("struct Fake: ReceiptPort {}")"
let pattern = #/struct Fake: ReceiptPort \{\}/#
''',
        protocol: "public protocol ReceiptPort {}\nprivate protocol PrivatePort {}\nprotocol Shadowed {}",
        "Packages/A/Sources/Payments/Sibling.swift": "protocol Shadowed {}\n",
        "Packages/B/Sources/Payments/CrossPackage.swift": "protocol ReceiptPort {}\nprotocol Other {}",
        "Packages/A/Sources/PaymentsAPI/CrossModule.swift": "protocol ReceiptPort {}\nprotocol Protocol {}",
        "Packages/A/Tests/Payments/Testing.swift": "protocol ReceiptPort {}\n",
        "manual/Outside.swift": "struct HiddenPort: ReceiptPort {}\n",
    }
    manifest = {path: extract(path, "object", text) for path, text in files.items()}
    assert resolved_implementations(file, manifest, set(files)) == [
        {"name": "PaymentAdapter", "interface": "LocalPort", "line": 2, "target": file},
        {"name": "PaymentAdapter", "interface": "ReceiptPort", "line": 2, "target": protocol},
        {"name": "Container.Receipt", "interface": "ReceiptPort", "line": 5, "target": protocol},
    ]
    assert resolved_implementations("manual/Outside.swift", manifest, set(files)) == []
    assert [item["name"] for item in manifest[file]["interfaces"]] == ["LocalPort"]


def test_configuration_extracts_names_only_at_real_declarations() -> None:
    content = """
# TOKEN_VALUE = fake-key
[ledger] # primary section
LEDGER_ADAPTER = "private-value-fixture"
MESSAGE = """
    content += '"""\n[not_a_section]\nFAKE_TOKEN = "fixture"\n"""\n'
    content += '[[workers]]\nWORKER_KEY = "value"\n[broken]]\nMISSING = # comment\n'
    symbols = extract("resources/ledger.toml", "object", content)["symbols"]
    assert [(item["name"], item["line"]) for item in symbols] == [
        ("ledger", 3), ("LEDGER_ADAPTER", 4), ("MESSAGE", 5), ("workers", 9), ("WORKER_KEY", 10),
    ]
    for path in ("nested/gateway.ini", "sandbox/gateway.conf"):
        facts = extract(path, "object", "[gateway]\nGATEWAY_PORT = private-value-fixture\n; IGNORED = value\n")
        assert [(item["name"], item["kind"]) for item in facts["symbols"]] == [
            ("gateway", "config"), ("GATEWAY_PORT", "config"),
        ]
        assert "private-value-fixture" not in repr(facts)


async def test_swift_and_configuration_publish_recall_and_update(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "Swift 账单项目"))["id"])}
    other = {"project_id": str((await project(runs, "隔离配置项目"))["id"])}
    caller = "Sources/Billing/Payment.swift"
    protocol = "Sources/Billing/Contract.swift"
    settings = "resources/billing.toml"

    async def sync(
        key: str, files: dict[str, str], params: dict[str, str] = scope,
    ) -> tuple[UUID, int]:
        result = await runs.client.post(
            f"{root}/codebases/local", params=params, headers=runs.headers(), json={
                "source_key": key * 64, "name": key * 8, "candidates": len(files), "incremental": True,
                "files": [{"path": name, "text": value} for name, value in files.items()],
            },
        )
        assert result.status_code == 200, result.text
        return UUID(result.json()["codebase_id"]), int(result.json()["reused"])

    configs = {settings: '[billing]\nPAYMENT_ADAPTER = "fixture-private-value"\n'}
    providers = {
        caller: "struct PaymentAdapter: ReceiptPort {}\n",
        protocol: "public protocol ReceiptPort {}\n",
    }
    repository, _ = await sync("a", providers)
    config, _ = await sync("b", configs)
    await sync("c", configs, other)
    page = str(uuid5(repository, caller))
    peer = str(uuid5(repository, protocol))
    cfg = str(uuid5(config, settings))

    async def detail(identity: str) -> dict[str, object]:
        result = await runs.client.get(f"{root}/{identity}", params=scope)
        assert result.status_code == 200, result.text
        assert "fixture-private-value" not in result.text
        return result.json()  # type: ignore[no-any-return]

    published = await detail(page)
    links = published["relations"]
    assert isinstance(links, list)
    assert {(link["kind"], link["target"]) for link in links} >= {("IMPLEMENTS", peer), ("DEPENDS_ON", cfg)}
    assert "ReceiptPort" in str(published["body"])
    assert "PAYMENT_ADAPTER" in str((await detail(cfg))["body"])

    # 上一版索引的对象哈希相同，也要重采集才能为旧项目补上新版的语言证据。
    async with runs.sessions.begin() as db:
        previous = await db.get(KnowledgeCodebase, repository)
        assert previous is not None
        previous.extraction_version = EXTRACTOR_VERSION - 1
    _, reused = await sync("a", providers)
    assert reused == 0

    result = await runs.client.get(root, params=scope | {"query": "PaymentAdapter", "recall_mode": "true"})
    assert any(
        row["id"] == page and {link["target"] for link in row["relations"]} >= {peer, cfg}
        for row in result.json()["items"]
    )

    _, reused = await sync("a", providers | {protocol: "struct ReceiptPort {}\n"})
    assert reused == 1
    links = (await detail(page))["relations"]
    assert isinstance(links, list) and all(link["kind"] != "IMPLEMENTS" for link in links)
    await sync("b", {settings: "[billing]\nANOTHER_KEY = 1\n"})
    links = (await detail(page))["relations"]
    assert isinstance(links, list) and all(link["target"] != cfg for link in links)
