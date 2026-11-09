"""校验构建缓存的版本/完整性拒绝逻辑，不把缓存单测当作真实浏览器验收。"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "runtimes/sandbox/install_browser_cache.py"
SPEC = importlib.util.spec_from_file_location("weora_browser_cache", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture_cache(cache: Path) -> dict[str, object]:
    """仅创建无真实浏览器的最小摘要夹具；构建集成另跑真实官方安装器。"""
    records: dict[str, object] = {}
    browsers: list[dict[str, str]] = []
    for name, filename, revision in (
        ("chromium-headless-shell", "chrome-headless-shell-linux64.zip", "1248"),
        ("ffmpeg", "ffmpeg-linux.zip", "1013"),
    ):
        body = name.encode()
        (cache / filename).write_bytes(body)
        records[name] = {
            "filename": filename,
            "revision": revision,
            "browser_version": "156.0.8078.4",
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        browsers.append({"name": name, "revision": revision, "browserVersion": "156.0.8078.4"})
    (cache / "manifest.json").write_text(json.dumps(records), encoding="utf-8")
    return {"browsers": browsers}


def test_missing_cache_uses_official_installer(tmp_path: Path) -> None:
    assert MODULE.verified_routes(tmp_path, {}) == {}


def test_verified_cache_has_only_two_fixed_routes(tmp_path: Path) -> None:
    registry = fixture_cache(tmp_path)
    routes = MODULE.verified_routes(tmp_path, registry)
    assert set(routes) == {
        "/builds/cft/156.0.8078.4/linux64/chrome-headless-shell-linux64.zip",
        "/builds/ffmpeg/1013/ffmpeg-linux.zip",
    }


@pytest.mark.parametrize("change", ["digest", "size", "revision", "browser_version", "filename"])
def test_changed_cache_fails_closed(tmp_path: Path, change: str) -> None:
    registry = fixture_cache(tmp_path)
    manifest = tmp_path / "manifest.json"
    records = json.loads(manifest.read_text(encoding="utf-8"))
    record = records["chromium-headless-shell"]
    record[{"digest": "sha256"}.get(change, change)] = 999 if change == "size" else "0" * 64
    manifest.write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError):
        MODULE.verified_routes(tmp_path, registry)


def test_incomplete_cache_does_not_fall_back_to_network(tmp_path: Path) -> None:
    registry = fixture_cache(tmp_path)
    (tmp_path / "ffmpeg-linux.zip").unlink()
    with pytest.raises(ValueError):
        MODULE.verified_routes(tmp_path, registry)
