"""Git 进程契约与网络边界；Fake 只用于安全用例，真实 HTTPS 联调单列。"""

import asyncio
import base64
import subprocess
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
    (root / "edu").mkdir()
    (root / "edu" / "ExamService.java").write_text("class ExamService {}\n// AssemblePaper\n", encoding="utf-8")
    (root / "edu" / ".env").write_text("AssemblePaper private fixture\n", encoding="utf-8")
    (root / "other").mkdir()
    (root / "other" / "OtherExam.java").write_text("// AssemblePaper\n", encoding="utf-8")
    git("add", "-f", "edu", "other")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "initial")
    revision = git("rev-parse", "HEAD")
    (root / "edu" / "LaterExam.java").write_text("// AssemblePaper\n", encoding="utf-8")
    git("add", "edu/LaterExam.java")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "later")

    reader = GitRepositoryReader(tmp_path)
    assert (await reader.directory(cache_id, revision, "edu"))["entries"] == [
        {"name": "ExamService.java", "kind": "blob"}
    ]
    assert (await reader.find(cache_id, revision, "edu", "*Exam*.java"))["paths"] == ["edu/ExamService.java"]
    assert (await reader.search(cache_id, revision, "edu", "assemblepaper"))["matches"] == [
        {"path": "edu/ExamService.java", "line": 2, "text": "// AssemblePaper"}
    ]
    assert (await reader.search(cache_id, revision, "", "-nonexistent"))["matches"] == []
    with pytest.raises(ProjectError, match="文件路径不可读取"):
        await reader.search(cache_id, revision, "../other", "AssemblePaper")


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
    with pytest.raises(ProjectError, match="64 KiB"):
        await reader.read(uuid4(), REVISION, "README.md")
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
