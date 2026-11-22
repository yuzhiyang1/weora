"""缺口阈值随当前索引证据变化。"""

from weora_code_graph.coverage import detect_gaps
from weora_code_graph.extractor import extract
from weora_code_graph.javascript_index import JavaScriptIndex


def test_detected_gap_thresholds_follow_code_evidence() -> None:
    interfaces = "export interface FirstPort {}\nexport interface SecondPort {}\n"
    interfaces += "export interface ThirdPort {}\nexport interface FourthPort {}\n"
    manifest = {
        **{
            f"src/item_{item}.py": extract(
                f"src/item_{item}.py", "f" * 64, "def first(): return 1\ndef second(): return 2\n",
            )
            for item in range(6)
        },
        "src/ports.ts": extract("src/ports.ts", "e" * 64, interfaces),
    }
    edges: dict[str, list[str]] = {path: [] for path in manifest}
    initial = detect_gaps(manifest, edges, set(), JavaScriptIndex(manifest, set(edges)), candidates=8)
    assert {finding["kind"] for finding in initial} == {
        "IMPL_MISSING", "LOW_CONNECTIVITY", "ERROR_HANDLING_UNDOCUMENTED", "CONFIG_UNDOCUMENTED", "SCAN_TRUNCATED",
    }

    manifest["settings.toml"] = extract("settings.toml", "a" * 64, '[network]\nENDPOINT = "localhost"\n')
    manifest["src/item_0.py"] = extract("src/item_0.py", "b" * 64, "class DispatchError(Exception): pass\n")
    edges = {path: ["settings.toml"] for path in manifest if path != "settings.toml"} | {"settings.toml": []}
    repaired = detect_gaps(
        manifest, edges, {("src/ports.ts", "FirstPort")}, JavaScriptIndex(manifest, set(edges)), candidates=len(edges),
    )
    assert repaired == []
