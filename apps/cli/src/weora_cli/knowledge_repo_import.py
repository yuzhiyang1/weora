"""从仓库清单或组织生成项目索引计划，并逐仓库保存批量导入结果。"""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from weora_code_graph.design import assign_code_domain

from weora_cli.collab_client import Client, CollabError, as_object, items, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_code_enrichment import enrich_platform
from weora_cli.knowledge_import_enrichment import (
    ImportDesignDriver,
    finish_batch_designs,
    indexed_for_design,
    saved_design_driver,
)
from weora_cli.knowledge_rule_import import _locked, _state_path

_MAX_CONFIG_BYTES = 10 * 1024 * 1024
_PLAN_PAGE = 200
_PRIORITIES = {"high": 0, "normal": 1, "low": 2}


def positive_option(value: int | None, label: str, default: int) -> int:
    count = default if value is None else value
    if not 1 <= count <= (1 << 53) - 1:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", f"{label} 必须是安全范围内的正整数。")
    return count


def pattern(value: str | None) -> re.Pattern[str] | None:
    if value is None:
        return None
    try:
        return re.compile(value)
    except re.error as error:
        raise CollabError("KNOWLEDGE_ORG_PATTERN_INVALID", "组织仓库筛选正则表达式无效。") from error


def repository_reference(value: str) -> str:
    """仓库输入只用于定位登记，拒绝携带密钥的地址。"""
    clean = value.strip()
    try:
        return str(UUID(clean))
    except ValueError:
        pass
    try:
        address = clean
        if clean.startswith("git@") and ":" in clean and "://" not in clean:
            host, route = clean.split(":", 1)
            address = f"ssh://{host}/{route}"
        url = urlsplit(address)
        _ = url.port
        if (
            len(clean) <= 2048
            and url.hostname
            and url.path.strip("/")
            and not url.query
            and not url.fragment
            and url.password is None
            and not any(char.isspace() or ord(char) < 32 or char == "\\" for char in clean)
            and ((url.scheme == "https" and url.username is None) or (url.scheme == "ssh" and url.username == "git"))
        ):
            return clean
    except ValueError:
        pass
    raise CollabError("KNOWLEDGE_REPO_REFERENCE_INVALID", "请使用登记仓库 ID 或不带凭据的 HTTPS／SSH 仓库地址。")


@dataclass(frozen=True, slots=True)
class RepositoryImportSource:
    """清单中的仓库引用。离线导入使用认证方式选择本机的 Git 传输。"""

    source: str
    auth: str | None = None
    domain: str | None = None


def read_repo_list(path: Path) -> tuple[list[RepositoryImportSource], list[JsonValue]]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError
        value: object = YAML(typ="safe").load(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError, YAMLError):
        raise CollabError("KNOWLEDGE_REPO_LIST_INVALID", "无法读取有效的仓库清单（最多 10 MB）。") from None
    if not isinstance(value, dict):
        raise CollabError("KNOWLEDGE_REPO_LIST_INVALID", "仓库清单需要使用 version: 1 和 repos 列表。")
    version, entries = value.get("version", 1), value.get("repos", [])
    if type(version) is not int or version != 1 or not isinstance(entries, list):
        raise CollabError("KNOWLEDGE_REPO_LIST_INVALID", "仓库清单需要使用 version: 1 和 repos 列表。")
    chosen: list[tuple[int, RepositoryImportSource]] = []
    skipped: list[JsonValue] = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise _invalid_entry(index)
        if "org" in entry and "url" not in entry and "repository_id" not in entry:
            if not isinstance(entry["org"], str):
                raise _invalid_entry(index)
            skipped.append({"row": index, "status": "skipped", "code": "ORG_ENTRY_REQUIRES_FROM_ORG"})
            continue
        source = entry.get("url", entry.get("repository_id"))
        priority = entry.get("priority", "normal")
        auth = entry.get("auth")
        domain = entry.get("domain", "")
        if (
            not isinstance(source, str)
            or not isinstance(priority, str)
            or priority not in _PRIORITIES
            or (auth is not None and (not isinstance(auth, str) or auth not in {"token", "ssh", "public"}))
            or not isinstance(domain, str)
            or ("iwiki_space" in entry and not isinstance(entry["iwiki_space"], str))
        ):
            raise _invalid_entry(index)
        profile = assign_code_domain(domain) if domain else None
        if domain and profile is None:
            raise _invalid_entry(index)
        try:
            chosen.append((
                _PRIORITIES[priority],
                RepositoryImportSource(repository_reference(source), auth, profile.domain if profile else None),
            ))
        except CollabError as error:
            raise _invalid_entry(index) from error
    chosen.sort(key=lambda row: row[0])
    return [source for _, source in chosen], skipped


def repository_domains(sources: list[RepositoryImportSource]) -> dict[str, str]:
    # 同一来源重复时，与仓库执行计划一样采用清单中优先级最高的那条配置。
    first = {item.source: item for item in reversed(sources)}
    return {source: item.domain for source, item in first.items() if item.domain is not None}


def load_repo_list(path: Path) -> tuple[list[str], list[JsonValue]]:
    sources, skipped = read_repo_list(path)
    return list(dict.fromkeys(item.source for item in sources)), skipped


def _invalid_entry(index: int) -> CollabError:
    return CollabError("KNOWLEDGE_REPO_LIST_INVALID", f"仓库清单第 {index} 项格式无效。")


def write_whitelist(path: Path, rows: list[dict[str, JsonValue]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", newline="\n", delete=False) as stream:
        staged = Path(stream.name)
    try:
        with staged.open("w", encoding="utf-8", newline="\n") as stream:
            dumper = YAML(typ="safe")
            dumper.default_flow_style = False
            dumper.dump({"version": 1, "repos": rows}, stream)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def repository_plan(client: Client, project: str, sources: list[str]) -> list[dict[str, JsonValue]]:
    plan: list[dict[str, JsonValue]] = []
    for offset in range(0, len(sources), _PLAN_PAGE):
        page = sources[offset : offset + _PLAN_PAGE]
        values: list[JsonValue] = [source for source in page]
        result = items(call(client, project, "/codebases/repo-plan", method="POST", payload={"sources": values}))
        if [row.get("source") for row in result] != page:
            raise CollabError("KNOWLEDGE_REPO_PLAN_INVALID", "平台返回的仓库计划与请求不一致。")
        plan.extend(result)
    return plan


def _identity(row: dict[str, JsonValue]) -> tuple[str, int] | None:
    repository, version = row.get("repository_id"), row.get("connection_version")
    if isinstance(repository, str) and type(version) is int and version > 0:
        try:
            return str(UUID(repository)), version
        except ValueError:
            pass
    return None


def preview_repositories(
    client: Client,
    project: str,
    sources: list[str],
    skipped: list[JsonValue],
    *, domain_overrides: dict[str, str] | None = None,
) -> dict[str, JsonValue]:
    rows = repository_plan(client, project, sources)
    for row in rows:
        if domain := (domain_overrides or {}).get(str(row["source"])):
            row["domain"] = domain
    report = _summary(rows, skipped) | {"preview": True}
    if report["failed"]:
        raise CollabError("KNOWLEDGE_CODEBASE_BATCH_INCOMPLETE", "部分仓库未能生成导入计划。", report)
    return report


def _summary(rows: list[dict[str, JsonValue]], skipped: list[JsonValue]) -> dict[str, JsonValue]:
    return {
        "items": [*rows, *skipped],
        "succeeded": sum(row.get("status") == "succeeded" for row in rows),
        "ready": sum(row.get("status") == "ready" for row in rows),
        "failed": sum(row.get("status") == "failed" for row in rows),
        "skipped": len(skipped) + sum(row.get("status") == "skipped" for row in rows),
    }


def is_repo_batch(client: Client, run: UUID) -> bool:
    path = _state_path(client, run)
    return path.is_file() and as_object(json.loads(path.read_text(encoding="utf-8"))).get("repo_batch") is True


def import_repositories(
    client: Client,
    *,
    project: str | None = None,
    sources: list[str] | None = None,
    incremental: bool = False,
    limit: int | None = None,
    concurrency: int = 3,
    skipped: list[JsonValue] | None = None,
    whitelist: str | None = None,
    design_driver: ImportDesignDriver = False,
    domain_overrides: dict[str, str] | None = None,
    resume: UUID | None = None,
) -> dict[str, JsonValue]:
    run = resume or uuid4()
    path = _state_path(client, run)
    with _locked(path):
        if resume:
            state = as_object(json.loads(path.read_text(encoding="utf-8")))
            if "result" in state:
                return as_object(state["result"])
        else:
            state = {
                "repo_batch": True,
                "project": project,
                "sources": [source for source in (sources or [])],
                "incremental": incremental,
                "limit": limit,
                "concurrency": concurrency,
                "design_driver": design_driver,
                "domain_overrides": dict(domain_overrides or {}),
                "skipped": skipped or [],
                "done": {},
                "whitelist": whitelist,
            }
            write_json(path, state)
            print(f"仓库导入运行 {run}，可使用 weora context import --resume {run} 续做。", file=sys.stderr)
        return _run_batch(client, run, path, state)


def _run_batch(client: Client, run: UUID, path: Path, state: dict[str, JsonValue]) -> dict[str, JsonValue]:
    project, workers, sources = state.get("project"), state.get("concurrency"), state.get("sources")
    assert isinstance(project, str) and isinstance(workers, int) and isinstance(sources, list)
    done = as_object(state["done"])
    remaining = [
        str(source)
        for source in sources
        if str(source) not in done or (
            (row := as_object(done[str(source)])).get("status") not in {"succeeded", "skipped"}
            and not indexed_for_design(row)
        )
    ]
    skipped = state.get("skipped")
    assert isinstance(skipped, list)
    try:
        plan = repository_plan(client, project, remaining)
    except CollabError as error:
        raise CollabError(error.code, str(error), {"run_id": str(run), "error": error.data}) from error
    used = {
        target
        for item in done.values()
        if ((row := as_object(item)).get("status") == "succeeded" or indexed_for_design(row))
        and (target := _identity(row)) is not None
    }
    selected: list[tuple[str, tuple[str, int]]] = []
    for row in plan:
        source, status = str(row["source"]), row.get("status")
        target = _identity(row)
        if status != "ready" or target is None:
            done[source] = row | {"status": "failed", "code": row.get("code", "INVALID_RESPONSE")}
        elif target in used:
            done[source] = row | {"status": "skipped", "code": "DUPLICATE_REPOSITORY"}
        else:
            used.add(target)
            selected.append((source, target))
    write_json(path, state)
    if selected:
        _execute_batch(client, project, run, path, state, done, selected, workers)
    driver = saved_design_driver(state)
    if driver is not False or state.get("domain_overrides"):
        _enrich_repositories(client, project, driver, path, state, done, workers)
    rows = [as_object(done[str(source)]) for source in sources]
    report = _summary(rows, skipped) | {"run_id": str(run)}
    if isinstance(whitelist := state.get("whitelist"), str):
        report["whitelist"] = whitelist
    if report["failed"]:
        raise CollabError("KNOWLEDGE_CODEBASE_BATCH_INCOMPLETE", "部分仓库导入失败，可使用原运行 ID 续做。", report)
    write_json(path, {"repo_batch": True, "result": report})
    return report


def _enrich_repositories(
    client: Client, project: str, driver: ImportDesignDriver, path: Path, state: dict[str, JsonValue],
    done: dict[str, JsonValue], workers: int,
) -> None:
    reader = replace(client, timeout=120)

    def generate(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
        domain = row.get("domain")
        return enrich_platform(
            reader, project, f"/codebases/{row['repository_id']}/design",
            driver=driver, max_modules=0, concurrency=5,
            preview=False, expected_index=as_object(row["result"]),
            domain_override=domain if isinstance(domain, str) else None,
        )

    finish_batch_designs(path, state, done, workers, generate)


def _execute_batch(
    client: Client,
    project: str,
    run: UUID,
    path: Path,
    state: dict[str, JsonValue],
    done: dict[str, JsonValue],
    selected: list[tuple[str, tuple[str, int]]],
    workers: int,
) -> None:
    # 每个仓库独立提交。HTTP 响应丢失时，同一运行用相同操作 ID 取回已提交的结果。
    reader = replace(client, timeout=140)
    options = {"incremental": state.get("incremental") is True}
    driver = saved_design_driver(state)
    domains = as_object(state.get("domain_overrides", {}))
    with ThreadPoolExecutor(max_workers=min(workers, len(selected))) as executor:
        jobs = {}
        for source, (repository, version) in selected:
            payload: dict[str, JsonValue] = {
                **options,
                "expected_connection_version": version,
                "operation_id": str(uuid5(run, f"{repository}:{version}")),
            }
            if isinstance(limit := state.get("limit"), int):
                payload["max_files"] = limit
            future = executor.submit(call, reader, project, f"/codebases/{repository}", method="POST", payload=payload)
            jobs[future] = (source, repository, version)
        for future in as_completed(jobs):
            source, repository, version = jobs[future]
            row: dict[str, JsonValue] = {"source": source, "repository_id": repository, "connection_version": version}
            domain = domains.get(source)
            if isinstance(domain, str):
                row["domain"] = domain
            try:
                indexed = "indexed" if driver is not False or domain else "succeeded"
                row |= {"status": indexed, "result": future.result()}
            except CollabError as error:
                row |= {"status": "failed", "code": error.code}
            done[source] = row
            write_json(path, state)
