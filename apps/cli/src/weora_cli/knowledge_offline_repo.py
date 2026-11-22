"""读取固定的远端 Git 对象，按来源发布和恢复离线代码批次。"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from weora_cli.collab_client import CollabError, as_object, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_code_enrichment import enrich_offline_snapshot
from weora_cli.knowledge_import_enrichment import (
    ImportDesignDriver,
    finish_batch_designs,
    indexed_for_design,
    saved_design_driver,
)
from weora_cli.knowledge_local_code import (
    _MAX_FILE_BYTES,
    _MAX_TOTAL_BYTES,
    decode_source,
    source_path,
    source_priority,
)
from weora_cli.knowledge_mr import Provider, _authorization
from weora_cli.knowledge_offline_code import (
    OfflineCodeWriter,
    offline_code_writer,
    remote_baseline,
    repository_identity,
)
from weora_cli.knowledge_repo_import import RepositoryImportSource, _summary, repository_domains
from weora_cli.knowledge_rule_import import _locked, _state_path
from weora_cli.knowledge_session_origin import _safe_remote

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_MAX_TREE_BYTES = 32 * 1024 * 1024
_BATCH_MARKER = "offline_repo_batch"


@dataclass(frozen=True, slots=True)
class RemoteRepository:
    address: str
    transport: str
    key: str
    auth: str | None


@dataclass(frozen=True, slots=True)
class GitHead:
    sha: str
    branch: str | None


@dataclass(frozen=True, slots=True)
class GitFile:
    path: str
    oid: str
    size: int


@dataclass(frozen=True, slots=True)
class PreparedRepository:
    """读取完成后交给主线程发布，所有写操作遵守同一 teamwiki 锁。"""

    snapshot: dict[str, JsonValue] | None
    unchanged: dict[str, JsonValue] | None = None


def _repository(item: RepositoryImportSource) -> RemoteRepository:
    address = _safe_remote(item.source.strip())
    if address is None:
        raise CollabError("KNOWLEDGE_OFFLINE_REMOTE_REQUIRED", "离线导入需要无内嵌凭据的 HTTPS 或 SSH 仓库地址。")
    url = urlsplit(address)
    transport = address
    if item.auth == "ssh" and url.scheme == "https":
        if url.port not in {None, 443}:
            raise CollabError("KNOWLEDGE_OFFLINE_SSH_ADDRESS", "非标准端口的仓库请直接提供 SSH 地址。")
        host = "gitcode.com:2222" if url.hostname == "gitcode.com" else url.netloc.removesuffix(":443")
        transport = f"ssh://git@{host}{url.path}"
    if item.auth == "public" and url.scheme != "https":
        raise CollabError("KNOWLEDGE_OFFLINE_PUBLIC_ADDRESS", "公开仓库模式需使用 HTTPS 地址。")
    identity, _ = repository_identity(address)
    return RemoteRepository(address, transport, hashlib.sha256(identity.encode()).hexdigest(), item.auth)


def _git_token(address: str) -> tuple[str, str] | None:
    url = urlsplit(address)
    provider: Provider
    host = url.netloc.lower().removesuffix(":443")
    if host == "github.com":
        provider, login = "github", "x-access-token"
    elif host == "gitcode.com":
        provider, login = "gitcode", "oauth2"
    else:
        return None
    try:
        credentials = _authorization(provider)
    except CollabError as exc:
        if exc.code == "KNOWLEDGE_MR_CREDENTIAL_REQUIRED":
            return None
        raise
    token = credentials.get("PRIVATE-TOKEN") or credentials.get("Authorization", "").removeprefix("Bearer ")
    return (login, token) if token else None


def _git_env(repo: RemoteRepository | None) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_TRACE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        and key not in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_PARAMETERS", "GIT_CURL_VERBOSE",
        }
    }
    env.update({
        "GIT_ALLOW_PROTOCOL": "" if repo is None else urlsplit(repo.transport).scheme,
        "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_CONFIG_COUNT": "0",
    })
    if repo is None:
        return env

    settings = {"http.followRedirects": "false"}
    scheme = urlsplit(repo.transport).scheme
    if scheme == "ssh":
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    if repo.auth == "public":
        settings.update({"credential.helper": "", "http.extraHeader": "", "core.askPass": ""})
        env.update({"GIT_ASKPASS": "", "SSH_ASKPASS": ""})
    elif scheme == "https" and (credential := _git_token(repo.transport)) is not None:
        # 令牌只通过本次 Git 进程的仓库级 HTTP 配置传递，不写入 URL、日志或裸库配置。
        login, token = credential
        basic = base64.b64encode(f"{login}:{token}".encode()).decode()
        settings[f"http.{repo.transport.rstrip('/')}.extraHeader"] = f"Authorization: Basic {basic}"
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _git_bytes(
    cwd: Path, env: dict[str, str], *arguments: str, maximum: int, timeout: int = 45, payload: bytes | None = None,
) -> bytes:
    git = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", *arguments]
    try:
        # stdout 落在短时临时文件。超大目录树不能导致 CLI 无界读取内存。
        with TemporaryFile() as output:
            process = subprocess.run(
                git, cwd=cwd, env=env, input=payload, stdin=subprocess.DEVNULL if payload is None else None,
                stdout=output, stderr=subprocess.DEVNULL, check=False, timeout=timeout,
            )
            if process.returncode:
                raise CollabError("KNOWLEDGE_OFFLINE_GIT", "无法读取仓库 Git 对象，请检查地址、访问权限及本机 Git。")
            output.seek(0)
            raw = output.read(maximum + 1)
    except subprocess.TimeoutExpired as exc:
        raise CollabError("KNOWLEDGE_OFFLINE_GIT_TIMEOUT", "Git 读取超时，可按原运行 ID 重试。") from exc
    except OSError as exc:
        raise CollabError("KNOWLEDGE_OFFLINE_GIT", "无法运行 Git 或读取临时仓库。") from exc
    if len(raw) > maximum:
        raise CollabError("KNOWLEDGE_OFFLINE_GIT_LIMIT", "仓库对象清单超过离线读取上限。")
    return raw


def _head(raw: bytes) -> GitHead:
    try:
        sha = ""
        branch = None
        for line in raw.decode().splitlines():
            value, separator, name = line.partition("\t")
            if separator and name == "HEAD":
                if value.startswith("ref: refs/heads/"):
                    branch = value.removeprefix("ref: refs/heads/") or None
                elif _SHA.fullmatch(value):
                    sha = value
        if sha:
            return GitHead(sha, branch)
    except UnicodeError:
        pass
    raise CollabError("KNOWLEDGE_OFFLINE_REMOTE_HEAD", "仓库默认分支缺少可读取的提交。")


def _remote_head(cwd: Path, repo: RemoteRepository, env: dict[str, str]) -> GitHead:
    return _head(_git_bytes(cwd, env, "ls-remote", "--symref", "--", repo.transport, "HEAD", maximum=4096))


def _fixed_head(cwd: Path, env: dict[str, str]) -> GitHead:
    sha = _git_bytes(cwd, env, "rev-parse", "--verify", "HEAD^{commit}", maximum=128).strip()
    branch = _git_bytes(cwd, env, "rev-parse", "--symbolic-full-name", "HEAD", maximum=4096).strip()
    return _head(b"ref: " + branch + b"\tHEAD\n" + sha + b"\tHEAD\n")


def _collect(cwd: Path, env: dict[str, str], head: GitHead) -> list[GitFile]:
    listing = _git_bytes(cwd, env, "ls-tree", "-r", "-l", "-z", "--full-tree", head.sha, maximum=_MAX_TREE_BYTES)
    candidates: list[GitFile] = []
    for row in listing.split(b"\x00"):
        header, separator, name = row.partition(b"\t")
        metadata = header.split()
        if not separator or len(metadata) != 4 or metadata[0] not in {b"100644", b"100755"}:
            continue
        _, kind, oid, size = metadata
        try:
            path = name.decode("utf-8")
            count, key = int(size), oid.decode("ascii")
        except (ValueError, UnicodeError):
            continue
        if kind == b"blob" and 0 <= count < _MAX_FILE_BYTES and _SHA.fullmatch(key) and source_path(path):
            candidates.append(GitFile(path, key, count))
    return sorted(candidates, key=lambda item: source_priority(item.path))


def _files(cwd: Path, env: dict[str, str], selected: list[GitFile]) -> list[JsonValue]:
    if not selected:
        return []
    if sum(blob.size for blob in selected) > _MAX_TOTAL_BYTES:
        raise CollabError("KNOWLEDGE_LOCAL_TOO_LARGE", "当前选择超过 8 MiB，请降低 --max-files。")
    if len({blob.path.casefold() for blob in selected}) != len(selected):
        raise CollabError("KNOWLEDGE_LOCAL_DUPLICATE", "仓库包含仅大小写不同的源码路径。")
    payload = "".join(f"{blob.oid}\n" for blob in selected).encode()
    raw = _git_bytes(
        cwd, env, "cat-file", "--batch", payload=payload, maximum=_MAX_TOTAL_BYTES + 96 * len(selected),
    )
    stream = BytesIO(raw)
    files: list[JsonValue] = []
    for file in selected:
        expected = f"{file.oid} blob {file.size}\n".encode()
        if stream.readline(96) != expected:
            raise CollabError("KNOWLEDGE_OFFLINE_GIT_OBJECT", "固定提交的 Git 对象与文件清单不一致。")
        content = stream.read(file.size)
        if len(content) != file.size or stream.read(1) != b"\n":
            raise CollabError("KNOWLEDGE_OFFLINE_GIT_OBJECT", "固定提交的 Git 对象长度与清单不一致。")
        try:
            text = decode_source(content)
        except ValueError as exc:
            raise CollabError(
                "KNOWLEDGE_LOCAL_UNREADABLE", f"Git 源码不是受支持的 UTF-8 文本（{file.path}）。",
            ) from exc
        files.append({"path": file.path, "text": text})
    return files


def _prepare(output: Path, repo: RemoteRepository, *, limit: int, incremental: bool) -> PreparedRepository:
    auth = _git_env(repo)
    with TemporaryDirectory(prefix="weora-codebase-") as directory:
        root = Path(directory)
        if incremental:
            known = _remote_head(root, repo, auth)
            baseline = remote_baseline(output, repo.address, repo.key, known.sha, known.branch, limit)
            if baseline is not None:
                return PreparedRepository(None, baseline)
        bare = root / "source.git"
        _git_bytes(
            root, auth, "clone", "--quiet", "--bare", "--depth=1", "--single-branch", "--no-tags",
            "--", repo.transport, str(bare), maximum=4096, timeout=180,
        )
        # 裸库只读对象。远端的子模块、链接、过滤器和源码都不会作为工作目录执行。
        env = _git_env(None)
        fixed = _fixed_head(bare, env)
        candidates = _collect(bare, env, fixed)
        files = _files(bare, env, candidates[:limit])
    snapshot: dict[str, JsonValue] = {
        "source_key": repo.key, "name": urlsplit(repo.address).path.rstrip("/").split("/")[-1].removesuffix(".git"),
        "max_files": limit, "candidates": len(candidates), "incremental": incremental, "files": files,
        "checkout": {
            "origin": repo.address, "commit": fixed.sha, "branch": fixed.branch,
            "subdir": "", "dirty": False, "verifiedRemote": True,
        },
    }
    return PreparedRepository(snapshot)


def _plan(sources: list[RepositoryImportSource]) -> tuple[dict[str, RemoteRepository], dict[str, JsonValue]]:
    selected: dict[str, RemoteRepository] = {}
    rejected: dict[str, JsonValue] = {}
    seen: set[str] = set()
    for item in sources:
        if item.source in selected or item.source in rejected:
            continue
        try:
            repo = _repository(item)
        except CollabError as error:
            rejected[item.source] = {"source": item.source, "status": "failed", "code": error.code}
            continue
        if repo.key in seen:
            rejected[item.source] = {"source": item.source, "status": "skipped", "code": "DUPLICATE_REPOSITORY"}
            continue
        seen.add(repo.key)
        selected[item.source] = repo
    return selected, rejected


def preview_offline_repositories(
    output: Path, sources: list[RepositoryImportSource], skipped: list[JsonValue], *,
    incremental: bool, limit: int,
) -> dict[str, JsonValue]:
    selected, results = _plan(sources)
    domains = repository_domains(sources)
    with TemporaryDirectory(prefix="weora-codebase-preview-") as directory:
        root = Path(directory)
        for source, repo in selected.items():
            try:
                head = _remote_head(root, repo, _git_env(repo))
                baseline = incremental and remote_baseline(output, repo.address, repo.key, head.sha, head.branch, limit)
                _, slug = repository_identity(repo.address)
                planned: dict[str, JsonValue] = {
                    "source": source, "status": "ready", "headSha": head.sha, "branch": head.branch,
                    "project": baseline["project"] if baseline else slug, "unchanged": bool(baseline),
                }
                if domain := domains.get(source):
                    planned["domain"] = domain
                results[source] = planned
            except CollabError as error:
                results[source] = {"source": source, "status": "failed", "code": error.code}
    report = _summary(_ordered(sources, results), skipped) | {"preview": True}
    if report["failed"]:
        raise CollabError("KNOWLEDGE_CODEBASE_BATCH_INCOMPLETE", "部分离线仓库无法确认读取计划。", report)
    return report


def _ordered(sources: list[RepositoryImportSource], results: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return [as_object(results[source]) for source in dict.fromkeys(item.source for item in sources)]


def start_offline_repositories(
    output: Path, sources: list[RepositoryImportSource], skipped: list[JsonValue], *,
    incremental: bool, limit: int, concurrency: int, whitelist: str | None = None,
    design_driver: ImportDesignDriver = False,
) -> dict[str, JsonValue]:
    run = uuid4()
    path = _state_path(None, run)
    rows: list[JsonValue] = [{"source": item.source, "auth": item.auth} for item in sources]
    domains: dict[str, JsonValue] = dict(repository_domains(sources))
    state: dict[str, JsonValue] = {
        _BATCH_MARKER: True, "output": str(output.resolve()), "sources": rows, "skipped": skipped,
        "incremental": incremental, "limit": limit, "concurrency": concurrency, "design_driver": design_driver,
        "domain_overrides": domains,
        "done": {}, "whitelist": whitelist,
    }
    with _locked(path):
        write_json(path, state)
        print(f"离线仓库导入运行 {run}，可使用 weora context import --resume {run} 续做。", file=sys.stderr)
        return _run(run, path, state)


def resume_offline_repositories(run: UUID) -> dict[str, JsonValue]:
    path = _state_path(None, run)
    with _locked(path):
        state = as_object(json.loads(path.read_bytes()))
        if "result" in state:
            return as_object(state["result"])
        return _run(run, path, state)


def _run(run: UUID, path: Path, state: dict[str, JsonValue]) -> dict[str, JsonValue]:
    rows, skipped, limit, workers = state["sources"], state["skipped"], state["limit"], state["concurrency"]
    assert isinstance(rows, list) and isinstance(skipped, list) and isinstance(limit, int) and isinstance(workers, int)
    sources = [
        RepositoryImportSource(str(row["source"]), auth if isinstance(auth := row["auth"], str) else None)
        for value in rows if isinstance(row := value, dict)
    ]
    output = Path(str(state["output"]))
    done = as_object(state["done"])
    selected, invalid = _plan(sources)
    done.update(invalid)
    pending = {
        name: repo for name, repo in selected.items()
        if (row := as_object(done.get(name, {}))).get("status") != "succeeded" and not indexed_for_design(row)
    }
    try:
        # 整个批次持锁。阅读多个来源可以并行，证据发布严格串行，公共图谱最后只重建一次。
        settled = [as_object(value) for value in done.values()]
        if pending or any(row.get("status") == "succeeded" or indexed_for_design(row) for row in settled):
            with offline_code_writer(output) as writer:
                try:
                    _execute(writer, output, pending, done, path, state, limit=limit, workers=workers)
                    driver = saved_design_driver(state)
                    if driver is not False or state.get("domain_overrides"):
                        _enrich_offline_repositories(writer, driver, path, state, done, workers)
                finally:
                    writer.refresh()
    except CollabError as error:
        raise CollabError(error.code, str(error), {"run_id": str(run), "error": error.data}) from error
    report = _summary(_ordered(sources, done), skipped) | {"run_id": str(run)}
    if isinstance(whitelist := state.get("whitelist"), str):
        report["whitelist"] = whitelist
    if report["failed"]:
        raise CollabError("KNOWLEDGE_CODEBASE_BATCH_INCOMPLETE", "部分离线仓库导入失败，可按原运行 ID 续做。", report)
    write_json(path, {_BATCH_MARKER: True, "result": report})
    return report


def _enrich_offline_repositories(
    writer: OfflineCodeWriter, driver: ImportDesignDriver, path: Path, state: dict[str, JsonValue],
    done: dict[str, JsonValue], workers: int,
) -> None:
    def generate(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
        result = as_object(row["result"])
        domain = row.get("domain")
        return enrich_offline_snapshot(
            writer.root, str(result["project"]), driver=driver, expected_revision=result["revision"],
            domain_override=domain if isinstance(domain, str) else None,
        )

    finish_batch_designs(path, state, done, workers, generate)


def _execute(
    writer: OfflineCodeWriter, output: Path, pending: dict[str, RemoteRepository],
    done: dict[str, JsonValue], state_path: Path, state: dict[str, JsonValue], *, limit: int, workers: int,
) -> None:
    if not pending:
        return
    driver = saved_design_driver(state)
    domains = as_object(state.get("domain_overrides", {}))
    with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as executor:
        # 每个并发窗口最多保留同等数量的源码快照，按清单优先级提交。
        for window in batched(pending.items(), workers):
            jobs = [
                (source, executor.submit(_prepare, output, repo, limit=limit, incremental=state["incremental"] is True))
                for source, repo in window
            ]
            for source, job in jobs:
                try:
                    prepared = job.result()
                    result = writer.publish(prepared.snapshot) if prepared.snapshot is not None else prepared.unchanged
                    domain = domains.get(source)
                    indexed = "indexed" if driver is not False or domain else "succeeded"
                    row: dict[str, JsonValue] = {"source": source, "status": indexed, "result": result}
                    if isinstance(domain, str):
                        row["domain"] = domain
                    done[source] = row
                except CollabError as error:
                    done[source] = {"source": source, "status": "failed", "code": error.code}
                write_json(state_path, state)
