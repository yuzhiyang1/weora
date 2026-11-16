"""Git 进程契约与网络边界；Fake 只用于安全用例，真实 HTTPS 联调单列。"""

import asyncio
import base64
import hashlib
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest

from weora.modules.projects.domain import ProjectError, RepositorySource, git_address
from weora.modules.projects.git_reader import GitRepositoryReader

REVISION = "a" * 40


@pytest.mark.anyio
async def test_repository_discovery_uses_fixed_git_objects(tmp_path: Path) -> None:
    """真实 Git 校验搜索协议，并验证新提交和被拒绝路径不会进入结果。"""
    cache_id = uuid4()
    root = tmp_path / str(cache_id)
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(("git", *args), cwd=root, stderr=subprocess.PIPE, text=True).strip()

    git("init", "--quiet")
    (root / "inventory").mkdir()
    (root / "inventory" / "SampleStock.java").write_text("class SampleStock {}\n// FixtureMarker\n", encoding="utf-8")
    (root / "inventory" / ".env").write_text("FixtureMarker in denied file\n", encoding="utf-8")
    (root / "other").mkdir()
    (root / "other" / "OtherStock.java").write_text("// FixtureMarker\n", encoding="utf-8")
    git("add", "-f", "inventory", "other")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "initial")
    revision = git("rev-parse", "HEAD")
    (root / "inventory" / "LaterStock.java").write_text("// FixtureMarker\n", encoding="utf-8")
    git("add", "inventory/LaterStock.java")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "later")

    reader = GitRepositoryReader(tmp_path)
    assert (await reader.directory(cache_id, revision, "inventory"))["entries"] == [
        {"name": "SampleStock.java", "kind": "blob"}
    ]
    assert (await reader.find(cache_id, revision, "inventory", "*Stock*.java"))["paths"] == [
        "inventory/SampleStock.java"
    ]
    assert (await reader.search(cache_id, revision, "inventory", "fixturemarker"))["matches"] == [
        {"path": "inventory/SampleStock.java", "line": 2, "text": "// FixtureMarker"}
    ]
    assert (await reader.search(cache_id, revision, "", "-nonexistent"))["matches"] == []
    with pytest.raises(ProjectError, match="文件路径不可读取"):
        await reader.search(cache_id, revision, "../other", "FixtureMarker")


@pytest.mark.parametrize("suffix", ["", "/", ".git", ".git/"])
def test_codeup_web_address_normalizes_to_clone_address_without_redirect(suffix: str) -> None:
    assert git_address("https://codeup.aliyun.com/org/team/repo" + suffix) == (
        "https://codeup.aliyun.com/org/team/repo.git"
    )
    assert git_address("https://git.example.test/team/repo") == "https://git.example.test/team/repo"


@pytest.mark.anyio
async def test_legacy_codeup_source_uses_canonical_url_for_git_and_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧配置在读边界修复，认证与 Git 请求必须使用同一规范地址，而不只改 DNS 解析。"""

    async def public_dns(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_dns)
    runner = Runner()
    reader = GitRepositoryReader(tmp_path, runner=runner)
    source = RepositorySource(uuid4(), uuid4(), "https://codeup.aliyun.com/org/team/repo", "main", "fixture", "pw")
    assert await reader.check(source) == REVISION
    await reader.clone(source, REVISION)
    for env, args in runner.calls:
        if args[0] in {"ls-remote", "fetch"}:
            assert "https://codeup.aliyun.com/org/team/repo.git" in args
            config = {env[k]: env[k.replace("KEY", "VALUE")] for k in env if k.startswith("GIT_CONFIG_KEY_")}
            assert "http.https://codeup.aliyun.com/org/team/repo.git.extraHeader" in config
            assert config["http.followRedirects"] == "false"


class Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[Mapping[str, str], tuple[str, ...]]] = []
        self.mode = b"100644"
        self.content = b"README-REAL-CONTENT"

    async def run(self, cwd: Path, env: Mapping[str, str], *args: str, stdin: bytes | None = None) -> bytes:
        self.calls.append((dict(env), args))
        if args[0] == "ls-remote":
            return (REVISION + "\trefs/heads/main\n").encode()
        if args[0] == "rev-parse":
            return REVISION.encode()
        if args[0] == "ls-tree":
            return self.mode + b" blob " + b"b" * 40 + b"\tREADME.md\0"
        if args[:2] == ("cat-file", "-s"):
            return str(len(self.content)).encode()
        if args[:2] == ("cat-file", "blob"):
            return self.content
        return b""


@pytest.mark.anyio
@pytest.mark.parametrize("secret", ["fixture-password:@/+!20", "fixture-access-token-20"])
async def test_https_accepts_git_password_or_token_without_exposing_either(tmp_path: Path, secret: str) -> None:
    """Git 密码不是 URL 片段：特殊字符应完整进入 Basic 密码位，且不得写入参数或缓存。"""
    runner = Runner()
    reader = GitRepositoryReader(tmp_path, runner=runner)
    source = RepositorySource(uuid4(), uuid4(), "https://93.184.216.34/team/a.git", "main", "git-user", secret)
    assert await reader.check(source) == REVISION
    env, args = runner.calls[0]
    config = {env[k]: env[k.replace("KEY", "VALUE")] for k in env if k.startswith("GIT_CONFIG_KEY_")}
    authorization = config["http.https://93.184.216.34/team/a.git.extraHeader"]
    assert base64.b64decode(authorization.removeprefix("Authorization: Basic ")).decode() == f"git-user:{secret}"
    assert secret not in repr(source) and secret not in str(args)
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


@pytest.mark.anyio
async def test_pinned_ip_isolation_fixed_fetch_and_blob_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Runner()
    reader = GitRepositoryReader(tmp_path, runner=runner)
    source = RepositorySource(
        uuid4(), uuid4(), "https://93.184.216.34/team/a.git", "main", "test-git", "ONLY-TEST-TOKEN"
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-proxy")
    monkeypatch.setenv("GIT_TRACE", "1")
    assert await reader.check(source) == REVISION
    cache_id = await reader.clone(source, REVISION)
    result = await reader.read(cache_id, REVISION, "README.md")
    assert result["content"] == "README-REAL-CONTENT" and result["revision"] == REVISION
    env, _ = runner.calls[0]
    assert "HTTPS_PROXY" not in env and "GIT_TRACE" not in env
    values = {env[k]: env[k.replace("KEY", "VALUE")] for k in env if k.startswith("GIT_CONFIG_KEY_")}
    assert values["http.curloptResolve"] == "93.184.216.34:443:93.184.216.34"
    assert values["http.followRedirects"] == "false" and values["http.sslVerify"] == "true"
    assert values["credential.helper"] == ""
    assert all("ONLY-TEST-TOKEN" not in str(args) for _, args in runner.calls)
    fetch = next(args for _, args in runner.calls if args[0] == "fetch")
    assert fetch[-1] == REVISION and "--no-recurse-submodules" in fetch
    assert not any(args[0] in {"checkout", "push", "submodule"} for _, args in runner.calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    ["../README.md", ".ENV", ".env.local", "a/.Git/config", "a\\b", "/README.md", ":(glob)*", "README.md ", "a\0b"],
)
async def test_file_paths_fail_before_git(tmp_path: Path, path: str) -> None:
    runner = Runner()
    reader = GitRepositoryReader(tmp_path, runner=runner)
    with pytest.raises(ProjectError, match="文件路径"):
        await reader.read(uuid4(), REVISION, path)
    assert runner.calls == []


@pytest.mark.anyio
async def test_private_dns_and_redirect_target_not_implicitly_allowed(tmp_path: Path) -> None:
    runner = Runner()
    reader = GitRepositoryReader(tmp_path, runner=runner)
    for url in ("https://127.0.0.1/private.git", "https://169.254.169.254/metadata.git"):
        with pytest.raises(ProjectError, match="私网或保留"):
            await reader.check(RepositorySource(uuid4(), uuid4(), url, "main"))
    assert runner.calls == []


@pytest.mark.anyio
async def test_symlink_large_binary_and_invalid_revision_are_rejected(tmp_path: Path) -> None:
    runner = Runner()
    reader = GitRepositoryReader(tmp_path, runner=runner)
    runner.mode = b"120000"
    with pytest.raises(ProjectError, match="符号链接"):
        await reader.read(uuid4(), REVISION, "README.md")
    runner.mode = b"100644"
    runner.content = b"x" * 65537
    page = await reader.read(uuid4(), REVISION, "README.md")
    assert page["truncated"] is True and page["next_line"] == 1 and page["next_column"] == 24000
    remainder = await reader.read_page(uuid4(), REVISION, "README.md", start_line=1, start_column=48000)
    assert remainder["content"] == "x" * (65537 - 48000) and remainder["truncated"] is False
    runner.content = b"a\0b"
    with pytest.raises(ProjectError, match="普通文本"):
        await reader.read(uuid4(), REVISION, "README.md")
    with pytest.raises(ProjectError, match="提交标识"):
        await reader.clone(RepositorySource(uuid4(), uuid4(), "https://93.184.216.34/a", "main"), "main")


@pytest.mark.anyio
async def test_clone_cancellation_discards_only_own_uuid_directory(tmp_path: Path) -> None:
    started = asyncio.Event()

    class Slow(Runner):
        async def run(self, cwd: Path, env: Mapping[str, str], *args: str, stdin: bytes | None = None) -> bytes:
            if args[0] == "fetch":
                started.set()
                await asyncio.Event().wait()
            return await super().run(cwd, env, *args, stdin=stdin)

    reader = GitRepositoryReader(tmp_path, runner=Slow())
    task = asyncio.create_task(
        reader.clone(RepositorySource(uuid4(), uuid4(), "https://93.184.216.34/a", "main"), REVISION)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


@pytest.mark.anyio
async def test_large_archive_is_stopped_without_discarding_synced_objects(tmp_path: Path) -> None:
    """本地裸库允许比沙箱更大的仓库，超限归档必须停止并只清理归档文件。"""
    stopped = asyncio.Event()

    def write_oversize_archive(target: Path) -> None:
        with target.open("wb") as archive:
            archive.truncate(64 * 1024 * 1024 + 1)

    class LargeArchive(Runner):
        async def run(self, cwd: Path, env: Mapping[str, str], *args: str, stdin: bytes | None = None) -> bytes:
            if args[0] != "archive":
                return await super().run(cwd, env, *args, stdin=stdin)
            target = Path(next(arg.removeprefix("--output=") for arg in args if arg.startswith("--output=")))
            await asyncio.to_thread(write_oversize_archive, target)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return b""

    cache_id = uuid4()
    root = tmp_path / str(cache_id)
    root.mkdir()
    retained = root / "HEAD"
    retained.write_text("ref: refs/weora/synced", encoding="utf-8")
    reader = GitRepositoryReader(tmp_path, runner=LargeArchive())
    with pytest.raises(ProjectError, match="归档超过沙箱预算"):
        async with asyncio.timeout(3):
            await reader.archive(cache_id, REVISION)
    assert stopped.is_set() and list(root.iterdir()) == [retained]


@pytest.mark.anyio
async def test_large_git_blob_is_read_in_pages_without_losing_line_numbers(tmp_path: Path) -> None:
    """真实 Git 超过进程普通输出限的对象仍可精读，工作树变化不影响固定提交。"""
    cache_id = uuid4()
    root = tmp_path / str(cache_id)
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(("git", *args), cwd=root, stderr=subprocess.PIPE, text=True).strip()

    git("init", "--quiet")
    lines = [f"// 第 {index} 行 " + "x" * 230 + "\n" for index in range(1, 12001)]
    (root / "Large.java").write_text("".join(lines), encoding="utf-8")
    git("add", "Large.java")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "large")
    revision = git("rev-parse", "HEAD")
    (root / "Large.java").write_text("工作树的新内容", encoding="utf-8")
    reader = GitRepositoryReader(tmp_path)
    first = await reader.read(cache_id, revision, "Large.java")
    assert first["total_lines"] == 12000 and first["truncated"] is True
    assert str(first["content"]).startswith(lines[0])
    middle = await reader.read_page(cache_id, revision, "Large.java", start_line=8000, max_lines=25)
    assert middle["content"] == "".join(lines[7999:8024])
    assert middle["next_line"] == 8025 and middle["next_column"] == 0
    final = await reader.read_page(cache_id, revision, "Large.java", start_line=11991, max_lines=25)
    assert final["content"] == "".join(lines[11990:]) and final["next_line"] is None
    assert first["sha256"] == middle["sha256"] == final["sha256"]
    # 单文件超过五个匹配仍可看到；结果上限必须真实标记，而非假装搜索完整。
    found = await reader.search(cache_id, revision, "Large.java", "第")
    assert len(found["matches"]) == 50 and found["truncated"] is True


@pytest.mark.anyio
async def test_native_archive_above_legacy_limit_keeps_entire_revision_with_streamed_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Git 导出超过 64MiB 的完整对象；默认仍拒绝，显式预算按精确字节边界判断。"""
    cache_id = uuid4()
    root = tmp_path / str(cache_id)
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(("git", *args), cwd=root, stderr=subprocess.PIPE, text=True).strip()

    git("init", "--quiet")
    size = 65 * 1024 * 1024
    with (root / "Large.bin").open("wb") as source:
        source.truncate(size)
        source.seek(size - 8)
        source.write(b"FULL-END")
    git("add", "Large.bin")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "large archive")
    revision = git("rev-parse", "HEAD")
    (root / "Large.bin").write_bytes(b"changed working tree")
    reader = GitRepositoryReader(tmp_path)

    def reject_whole_file(_path: Path) -> bytes:
        raise AssertionError("大归档不能通过 read_bytes 整体复制到内存")

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file)
    with pytest.raises(ProjectError, match="归档超过沙箱预算"):
        await reader.archive(cache_id, revision)
    assert not list(root.glob("weora-export-*.tar"))
    archive = await reader.archive(cache_id, revision, max_bytes=512 * 1024 * 1024)
    archive_size = archive.path.stat().st_size
    assert archive_size > 64 * 1024 * 1024 and archive.revision == revision
    with archive.path.open("rb") as source:
        assert archive.sha256 == hashlib.file_digest(source, "sha256").hexdigest()
    with tarfile.open(archive.path) as contents:
        assert contents.getmember("Large.bin").size == size
        exported = contents.extractfile("Large.bin")
        assert exported is not None
        with exported:
            exported.seek(size - 8)
            assert exported.read(8) == b"FULL-END"
    archive.path.unlink()
    exact = await reader.archive(cache_id, revision, max_bytes=archive_size)
    assert exact.sha256 == archive.sha256
    exact.path.unlink()
    with pytest.raises(ProjectError, match="归档超过沙箱预算"):
        await reader.archive(cache_id, revision, max_bytes=archive_size - 1)
    assert not list(root.glob("weora-export-*.tar"))
    assert (root / ".git" / "HEAD").is_file()
    for invalid in (0, -1, True):
        with pytest.raises(ProjectError, match="正整数字节数"):
            await reader.archive(cache_id, revision, max_bytes=invalid)


@pytest.mark.anyio
async def test_git_archive_excludes_restricted_paths_at_any_depth(tmp_path: Path) -> None:
    """新归档只交付可授权文件，签名必须与实际交付的 tar 字节一致。"""
    cache_id = uuid4()
    root = tmp_path / str(cache_id)
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(("git", *args), cwd=root, stderr=subprocess.PIPE, text=True).strip()

    git("init", "--quiet")
    for name in (".env", "config/.ENV.prod/secret.txt", "nested/.npmrc", "nested/id_rsa", "src/keep.py"):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture", encoding="utf-8")
    git("add", "-f", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "source")
    archive = await GitRepositoryReader(tmp_path).archive(cache_id, git("rev-parse", "HEAD"))
    assert archive.restricted_paths_removed
    with tarfile.open(archive.path) as tar:
        assert [entry.name for entry in tar if entry.isfile()] == ["src/keep.py"]
    with archive.path.open("rb") as saved:
        assert archive.sha256 == hashlib.file_digest(saved, "sha256").hexdigest()
