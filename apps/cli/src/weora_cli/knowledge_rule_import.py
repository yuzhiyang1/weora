"""从来源获取候选知识，并持续保存分类、审阅进度。"""

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4, uuid5

from weora_cli.collab_client import Client, CollabError, as_object, config_path, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_classifier import classify_rule, draft_review
from weora_cli.knowledge_import import review_import, review_markdown
from weora_cli.knowledge_iwiki import fetch_pages, prepare_fetch
from weora_cli.knowledge_iwiki_sections import baselines, draft_sections, write_sections
from weora_cli.knowledge_markdown import imported_metadata, imported_rule_paths, markdown_domain, rule_paths
from weora_cli.knowledge_mr import parse_target, read_review
from weora_cli.knowledge_offline_code import record_iwiki_mappings
from weora_cli.skill_adapters import tool_home
from weora_cli.skill_local import unsafe

# TeamAI 只扫描用户作用域使用文件目录的规则渠道。Codex 的 rules 是执行策略，Pi 的指令是内联文件。
RULE_DIRS = {
    "claude": "rules",
    "cursor": "rules",
    "copilot": "instructions",
    "codebuddy": "rules",
    "workbuddy": "rules",
    "qoder": "rules",
    "qoder-cn": "rules",
    "kiro": "steering",
    "trae": "user_rules",
    "trae-cn": "user_rules",
    "omp": "rules",
    "opencode": "rules",
    "agy": "rules",
}
MAX_RULE_BYTES = 50 * 1024
MAX_CANDIDATES = 200
MAX_REVIEWS = 25


def scan_rules() -> list[JsonValue]:
    """读取已知用户规则目录的 Markdown 或 MDC。发布出处仅带工具名和相对路径。"""
    results: list[JsonValue] = []
    roots: set[Path] = set()
    for tool, relative in RULE_DIRS.items():
        root = tool_home(tool) / relative
        if unsafe(root) or not root.is_dir() or root.resolve() in roots:
            continue
        root = root.resolve()
        roots.add(root)
        for directory, directories, files in root.walk():
            directories[:] = sorted(name for name in directories if not unsafe(directory / name))
            for name in sorted(files):
                file = directory / name
                if file.suffix.lower() not in {".md", ".mdc"} or unsafe(file):
                    continue
                try:
                    info = file.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RULE_BYTES:
                        continue
                    with file.open("rb") as stream:
                        raw = stream.read(MAX_RULE_BYTES + 1)
                    if len(raw) > MAX_RULE_BYTES or b"\x00" in raw:
                        continue
                    text = raw.decode("utf-8-sig")
                except (OSError, UnicodeError):
                    print(f"无法读取 {tool}/{file.relative_to(root).as_posix()}，已跳过。", file=sys.stderr)
                    continue
                if text[:3000].strip():
                    candidate: dict[str, JsonValue] = {
                        "ref": f"{tool}/{relative}/{file.relative_to(root).as_posix()}", "excerpt": text[:3000],
                    }
                    metadata = imported_metadata(text)
                    if (paths := rule_paths(metadata)) is not None:
                        candidate["rule_paths"] = paths if isinstance(paths, str) else list(paths)
                    if isinstance(metadata, dict) and (domain := markdown_domain(metadata.get("domain"))):
                        candidate["domain"] = domain
                    results.append(candidate)
                if len(results) > MAX_CANDIDATES:
                    raise CollabError("KNOWLEDGE_IMPORT_TOO_MANY", "每次可导入最多 200 个规则文件。")
    return results


def _state_path(client: Client | None, run: UUID) -> Path:
    # 环境变量设备令牌或本机账号切换时，旧设备的私人草稿不会进入新设备的运行。
    identity = hashlib.sha256(f"{client.account}\0{client.token}".encode()).hexdigest() if client else "local"
    return config_path().parent / "knowledge-import" / identity / f"{run}.json"


def is_local_import(run: str, *, marker: str | None = None) -> bool:
    path = _state_path(None, UUID(run))
    return path.is_file() and (marker is None or as_object(json.loads(path.read_bytes())).get(marker) is True)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    acquired = False
    try:
        with lock_file(path):
            acquired = True
            yield
    except OSError as exc:
        if not acquired:
            raise CollabError("KNOWLEDGE_IMPORT_RUNNING", "该导入正在另一个进程中执行。") from exc
        raise


def _requires_terminal(accept_all: bool) -> None:
    if not accept_all and not sys.stdin.isatty():
        raise CollabError("KNOWLEDGE_IMPORT_TERMINAL_REQUIRED", "逐条审阅需要终端，已确认整批时使用 --all。")


def run_import(
    client: Client | None,
    *,
    publish: Callable[[str | None, list[JsonValue]], dict[str, JsonValue]],
    project: str | None = None,
    driver: str | None = None,
    accept_all: bool = False,
    resume: str | None = None,
    output: Path | None = None,
    iwiki_source: str | None = None,
    iwiki_dual: bool = False,
    iwiki_require_review: bool = False,
    publish_summary: Callable[[str | None, dict[str, JsonValue]], dict[str, JsonValue]] | None = None,
    mr_sources: list[str] | None = None,
    discussions: bool = True,
    language: str = "zh",
    overlaps: Callable[[str | None, dict[str, JsonValue]], tuple[list[JsonValue], str | None]] | None = None,
    preview: bool = False,
    refresh: Callable[[str | None, UUID, list[JsonValue]], Iterator[dict[str, JsonValue]]] | None = None,
) -> dict[str, JsonValue]:
    """已分类和已审阅的条目逐项落盘，最后一次性提交确认过的批次。"""
    run = UUID(resume) if resume else uuid4()
    path = _state_path(client, run)
    if not resume and not preview:
        _requires_terminal(accept_all)
    with _locked(path):
        if resume:
            if not path.is_file():
                raise CollabError("KNOWLEDGE_IMPORT_NOT_FOUND", "当前设备找不到该导入。")
            state = as_object(json.loads(path.read_text(encoding="utf-8")))
            if "result" in state:
                return as_object(state["result"])
            if "iwiki_main_result" in state:
                if driver is not None:
                    state["driver"] = driver
                    write_json(path, state)
                return _finish_iwiki_summary(path, state, run, publish_summary)
        else:
            state = {
                "project": project,
                "driver": driver or "claude",
                "accept_all": accept_all,
                "candidates": [] if iwiki_source is not None or mr_sources is not None else scan_rules(),
                "output": str(output.resolve()) if output else None,
            }
            if iwiki_source is not None:
                fetch = prepare_fetch(iwiki_source)
                state["iwiki_fetch"] = fetch
                state["iwiki_preview"] = preview
                if iwiki_dual:
                    identity = fetch["seen"]
                    assert isinstance(identity, list) and len(identity) == 1
                    fetch["dual"] = True
                    state["iwiki_summary_source"] = f"iwiki://{identity[0]}"
                    state["iwiki_require_review"] = iwiki_require_review
            elif mr_sources is not None:
                refs = dict.fromkeys(parse_target(source).url for source in mr_sources)
                if not 1 <= len(refs) <= MAX_REVIEWS:
                    raise CollabError("KNOWLEDGE_MR_TOO_MANY", "单次可导入 1 至 25 个不同的 PR/MR。")
                state["mr_fetch"] = [{"ref": ref} for ref in refs]
                state["mr_discussions"] = discussions
                state["mr_language"] = language
                state["mr_preview"] = preview
                state["mr_markdown_version"] = 2
            elif not state["candidates"]:
                return {"items": [], "scanned": 0}
            write_json(path, state)
            print(f"导入运行 {run}，中断后可用 weora context import --resume {run} 续做。", file=sys.stderr)
        project_id = state.get("project")
        selected = state.get("candidates")
        assert isinstance(selected, list)
        scope = project_id if isinstance(project_id, str) else None
        should_accept_all = state.get("accept_all") is True
        iwiki = state.get("iwiki_fetch")
        reviews = state.get("mr_fetch")
        preview_only = state.get("mr_preview") is True or state.get("iwiki_preview") is True
        fetch_pending = (isinstance(iwiki, dict) and (iwiki["trees"] or iwiki["pages"])) or (
            isinstance(reviews, list) and reviews
        )
        if not preview_only and (fetch_pending or any("accepted" not in as_object(item) for item in selected)):
            _requires_terminal(should_accept_all)
        if driver is not None:
            state["driver"] = driver
            write_json(path, state)
        try:
            failures = fetch_pages(iwiki, selected, lambda: write_json(path, state)) if isinstance(iwiki, dict) else []
            if isinstance(reviews, list):
                failures = _fetch_reviews(
                    path, state, reviews, selected,
                    (lambda candidate: overlaps(scope, candidate)) if overlaps is not None else None,
                )
        except CollabError as error:
            raise CollabError(error.code, str(error), {"run_id": str(run), "error": error.data}) from error
        candidates = [as_object(candidate) for candidate in selected]
        _classify(path, state, candidates, run)
        accepted = [] if preview_only else _review_candidates(path, state, candidates, run, should_accept_all)
        result: dict[str, JsonValue]
        try:
            if preview_only:
                result = {"drafts": [
                    {"draft": item["classification"], "overlaps": item.get("overlaps", [])} for item in candidates
                ]}
            elif isinstance(directory := state.get("output"), str):
                written = _write_output(
                    Path(directory), accepted, review_markdown_v2=state.get("mr_markdown_version") == 2,
                )
                result = {"items": [], "written": written}
                if isinstance(iwiki, dict):
                    _link_offline_iwiki(result, Path(directory), written, accepted)
            else:
                result = publish(scope, accepted) if accepted else {"items": []}
        except CollabError as error:
            raise CollabError(error.code, str(error), {"run_id": str(run), "error": error.data}) from error
        result |= {"run_id": str(run), "scanned": len(candidates), "accepted": len(accepted)}
        if isinstance(iwiki, dict):
            result["truncated"] = iwiki["truncated"]
        if isinstance(reviews, list):
            result["warnings"] = [
                {"source": item["ref"], "details": item["warnings"]}
                for item in candidates if item.get("warnings")
            ]
            if not preview_only:
                result["overlaps"] = [
                    {"source": item["ref"], "titles": item["overlaps"]}
                    for item in candidates if item.get("overlaps")
                ]
            if refresh is not None and not preview_only and not isinstance(state.get("output"), str):
                result["codebase_refresh"] = _refresh_codebases(path, state, candidates, refresh, scope, run)
        if failures:
            if isinstance(reviews, list):
                raise CollabError(
                    "KNOWLEDGE_MR_IMPORT_INCOMPLETE",
                    "部分 PR/MR 未完成。可用原运行 ID 续做。",
                    {"run_id": str(run), "failures": failures, "result": result},
                )
            raise CollabError(
                "KNOWLEDGE_IWIKI_IMPORT_INCOMPLETE",
                "部分 iWiki 页面读取失败。可用原运行 ID 续做。",
                {"run_id": str(run), "failures": failures, "result": result},
            )
        if state.get("iwiki_summary_source"):
            state["iwiki_main_result"] = result
            write_json(path, state)
            return _finish_iwiki_summary(path, state, run, publish_summary)
        # 完成后只保留收据，原始规则及分类摘要无需继续在本机草稿目录保存。
        write_json(path, {"result": result})
        return result


def _link_offline_iwiki(
    result: dict[str, JsonValue], output: Path, written: list[JsonValue], accepted: list[JsonValue],
) -> None:
    files = []
    for filename, value in zip(written, accepted, strict=True):
        item = as_object(value)
        terms = item.get("iwiki_terms")
        source = str(as_object(item["source"])["ref"])
        labels = [str(term) for term in terms] if isinstance(terms, list) else []
        files.append((source, Path(str(filename)).name, labels))
    try:
        if count := record_iwiki_mappings(output, files):
            result["iwiki_code_mappings"] = count
    except (CollabError, OSError) as exc:
        # 与 TeamAI 一样，图谱不可写时保留已经成功导出的知识。
        code = exc.code if isinstance(exc, CollabError) else "KNOWLEDGE_OFFLINE_WRITE"
        result["iwiki_code_mapping_warning"] = code


def _finish_iwiki_summary(
    path: Path, state: dict[str, JsonValue], run: UUID,
    publish: Callable[[str | None, dict[str, JsonValue]], dict[str, JsonValue]] | None,
) -> dict[str, JsonValue]:
    """普通内容已落地。汇总失败保留原页，续做只重试生成或发布章节。"""
    result = as_object(state["iwiki_main_result"]).copy()
    source = str(state["iwiki_summary_source"])
    queue: dict[str, JsonValue] = {}
    try:
        saved = state.get("iwiki_sections")
        if not isinstance(saved, dict):
            candidates = state["candidates"]
            assert isinstance(candidates, list)
            saved = dict(draft_sections(str(state["driver"]), [as_object(item) for item in candidates]))
            state["iwiki_sections"] = saved
            write_json(path, state)
        sections = {key: value for key, value in saved.items() if isinstance(value, str)}
        rows: list[JsonValue] = [{"section": key, "body": body} for key, body in sections.items()]
        require_review = state.get("iwiki_require_review") is True
        if state.get("iwiki_preview") is True:
            result["iwiki_summary"] = {"status": "preview", "sections": rows}
        elif not sections:
            result["iwiki_summary"] = {"status": "empty", "sections": []}
        elif isinstance(directory := state.get("output"), str):
            status: dict[str, JsonValue]
            if require_review:
                bases = baselines(Path(directory), sections)
                pending: dict[str, JsonValue] = {
                    key: {"body": body, "baseline": bases[key]} for key, body in sections.items()
                }
                status = {"status": "pending", "sections": [{"section": key, "status": "pending"} for key in sections]}
                queue["iwiki_local_review"] = {
                    "source": source, "output": directory, "pending": pending, "receipts": {},
                }
            else:
                written = write_sections(Path(directory), sections, source)
                status = {"status": "written", "sections": list(sections), "path": written}
            result["iwiki_summary"] = status
        else:
            assert publish is not None
            project = state.get("project")
            published = publish(project if isinstance(project, str) else None, {
                "run_id": str(run), "source": source, "sections": rows, "require_review": require_review,
            })
            result["iwiki_summary"] = {"status": "submitted", "items": published.get("items", [])}
    except (OSError, CollabError) as error:
        code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_IWIKI_OUTPUT_FAILED"
        result["iwiki_summary"] = {"status": "failed", "code": code}
        print(f"iWiki 汇总未完成。可用 weora context import --resume {run} 续做。", file=sys.stderr)
        return result
    write_json(path, {"result": result, **queue})
    return result


def review_local_iwiki_section(run: UUID, section: str, decision: str, reason: str) -> dict[str, JsonValue]:
    path = _state_path(None, run)
    with _locked(path):
        if not path.is_file():
            raise CollabError("KNOWLEDGE_IMPORT_NOT_FOUND", "当前设备找不到该离线导入。")
        state = as_object(json.loads(path.read_bytes()))
        review = state.get("iwiki_local_review")
        if not isinstance(review, dict):
            raise CollabError("KNOWLEDGE_IWIKI_REVIEW_CONFLICT", "该导入没有离线章节审核记录。")
        pending = as_object(review.get("pending"))
        receipts = as_object(review.get("receipts", {}))
        if section in receipts:
            previous = as_object(receipts[section])
            if previous["decision"] == decision:
                return previous
        if section not in pending:
            raise CollabError("KNOWLEDGE_IWIKI_REVIEW_CONFLICT", "该章节没有待审核的离线草稿。")
        value = as_object(pending[section])
        if decision == "preview":
            return {"section": section, "status": "pending", "body": value["body"], "source": review["source"]}
        target = write_sections(
            Path(str(review["output"])), {section: str(value["body"])}, str(review["source"]),
            expected={section: str(value["baseline"])},
        ) if decision == "accept" else None
        item: dict[str, JsonValue] = {"section": section, "decision": decision, "path": target, "reason": reason}
        receipts[section] = item
        del pending[section]
        review["receipts"] = receipts
        result = as_object(state["result"])
        sections: list[JsonValue] = [{"section": key, "status": "pending"} for key in pending]
        sections.extend(receipts.values())
        result["iwiki_summary"] = {"status": "pending" if pending else "reviewed", "sections": sections}
        write_json(path, state)
        return item


def _refresh_codebases(
    path: Path, state: dict[str, JsonValue], candidates: list[dict[str, JsonValue]],
    refresh: Callable[[str | None, UUID, list[JsonValue]], Iterator[dict[str, JsonValue]]],
    scope: str | None, run: UUID,
) -> list[JsonValue]:
    """未接受的草稿仍可更新代码证据，来源读取或提炼失败的条目留给续做。"""
    stored = state.get("mr_codebase_refresh")
    history = [as_object(item) for item in stored] if isinstance(stored, list) else []
    complete: set[str] = set()
    for entry in history:
        if entry.get("status") in ("updated", "unchanged", "replayed", "skipped") and isinstance(
            urls := entry.get("urls"), list,
        ):
            complete.update(str(url) for url in urls)
    sources = [candidate["ref"] for candidate in candidates if str(candidate["ref"]) not in complete]
    if sources:
        for report in refresh(scope, run, sources):
            if isinstance(covered := report.get("urls"), list):
                _replace_review_results(history, covered)
            history.append(report)
            state["mr_codebase_refresh"] = [item for item in history]
            write_json(path, state)
    return [item for item in history]


def _replace_review_results(history: list[dict[str, JsonValue]], covered: list[JsonValue]) -> None:
    """续做时替换本次所覆盖的旧失败项，保留其他仓库的结果。"""
    for entry in history[:]:
        old = entry.get("urls")
        if isinstance(old, list) and any(url in covered for url in old):
            urls = [url for url in old if url not in covered]
            if urls:
                entry["urls"] = urls
            else:
                history.remove(entry)


def _review_candidates(
    path: Path, state: dict[str, JsonValue], candidates: list[dict[str, JsonValue]], run: UUID, accept_all: bool,
) -> list[JsonValue]:
    accepted: list[JsonValue] = []
    for item in candidates:
        if "accepted" not in item:
            result = item.get("classification")
            if isinstance(result, dict):
                if accept_all:
                    result = result.copy()
                else:
                    titles = item.get("overlaps")
                    result = review_import(result, similar_titles=titles if isinstance(titles, list) else None)
                if result is not None:
                    result["operation_id"] = str(uuid5(run, str(item["ref"])))
                    if item.get("iwiki_terms"):
                        result["iwiki_terms"] = item["iwiki_terms"]
            item["accepted"] = result
            item.pop("classification", None)
            write_json(path, state)
        if item["accepted"] is not None:
            accepted.append(item["accepted"])
    return accepted


def _fetch_reviews(
    path: Path,
    state: dict[str, JsonValue],
    reviews: list[JsonValue],
    selected: list[JsonValue],
    overlaps: Callable[[dict[str, JsonValue]], tuple[list[JsonValue], str | None]] | None,
) -> list[JsonValue]:
    """按链接保留读取和提炼进度，单条失败时继续处理其他来源。"""
    failures: list[JsonValue] = []
    for value in list(reviews):
        pending = as_object(value)
        ref = str(pending["ref"])
        facts = pending.get("facts")
        stage = "read"
        try:
            if not isinstance(facts, dict):
                facts = read_review(parse_target(ref), discussions=state.get("mr_discussions") is True)
                pending["facts"] = facts
                write_json(path, state)
            stage = "draft"
            draft = draft_review(str(state["driver"]), ref, facts, str(state["mr_language"]))
        except CollabError as error:
            failures.append({"source": ref, "stage": stage, "code": error.code})
            continue
        warnings = facts.get("warnings")
        candidate: dict[str, JsonValue] = {"ref": ref, "classification": draft, "warnings": warnings}
        if overlaps is not None:
            titles, warning = overlaps(draft)
            candidate["overlaps"] = titles
            if warning is not None:
                warnings = warnings if isinstance(warnings, list) else []
                candidate["warnings"] = warnings + [{"stage": "overlap", "code": warning}]
        selected.append(candidate)
        reviews.remove(value)
        write_json(path, state)
    return failures


def _write_output(
    directory: Path, items: list[JsonValue], *, review_markdown_v2: bool = False,
) -> list[JsonValue]:
    """同一运行可以重放本地输出，文件名和正文一致才可复用。"""
    written: list[JsonValue] = []
    if not items:
        return written
    directory.mkdir(parents=True, exist_ok=True)
    for value in items:
        item = as_object(value)
        stem = re.sub(r"[^\w-]+", "-", str(item["title"]).lower()).strip("-_")[:48] or "knowledge"
        target = directory / f"{stem}-{UUID(str(item['operation_id'])).hex}.md"
        source = as_object(item["source"])
        if source.get("type") == "pull_request" and review_markdown_v2:
            content = review_markdown(item)
        else:
            fields = {"title": item["title"], "type": item["kind"], "tags": item["tags"], "source": source["ref"]}
            if "domain" in item:
                fields["domain"] = item["domain"]
            if item["kind"] == "rule" and "rule_paths" in item:
                fields["paths"] = item["rule_paths"]
            # 已开始的旧运行可能写过部分文件，续做时按原格式重放，避免误报本地冲突。
            if source.get("type") == "pull_request":
                for field, key in (("author", "review_author"), ("date", "review_date")):
                    if key in source:
                        fields[field] = source[key]
            frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
            content = f"---\n{frontmatter}\n---\n\n{item['body']}\n"
        # 链接将完整临时文件原子化地放到目的地，且不会覆盖同名用户文件。
        with NamedTemporaryFile(mode="w", dir=directory, encoding="utf-8", newline="\n", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(content)
        try:
            os.link(staged, target)
        except FileExistsError as exc:
            if unsafe(target) or not target.is_file() or target.read_bytes() != content.encode():
                raise CollabError("KNOWLEDGE_IMPORT_OUTPUT_CONFLICT", f"请处理已有输出文件：{target.name}。") from exc
        finally:
            staged.unlink(missing_ok=True)
        written.append(str(target))
    return written


def _classify(
    path: Path,
    state: dict[str, JsonValue],
    candidates: list[dict[str, JsonValue]],
    run: UUID,
) -> None:
    pending = [item for item in candidates if "excerpt" in item]
    failures: list[JsonValue] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        jobs = {
            executor.submit(classify_rule, str(state["driver"]), str(item["ref"]), str(item["excerpt"])): item
            for item in pending
        }
        for future in as_completed(jobs):
            item = jobs[future]
            try:
                value = future.result()
            except CollabError as error:
                failures.append({"source": item["ref"], "code": error.code})
            else:
                if value is not None and (domain := markdown_domain(item.get("domain"))):
                    value["domain"] = domain
                if value is not None and value["kind"] == "rule":
                    paths = item.get("rule_paths")
                    if paths is None:
                        original = imported_rule_paths(str(item["excerpt"]))
                        paths = list(original) if isinstance(original, list) else original
                    if paths is not None:
                        value = value | {"rule_paths": paths}
                if value is not None and item.get("source_type") == "wiki":
                    value["source"] = {"type": "wiki", "ref": item["ref"]}
                    title = item.get("title_hint")
                    if (
                        value["title"] == str(item["ref"]).rsplit("/", 1)[-1]
                        and isinstance(title, str)
                        and "\x00" not in title
                    ):
                        value["title"] = title
                item["classification"] = value
                del item["excerpt"]
                item.pop("title_hint", None)
                write_json(path, state)
    if failures:
        raise CollabError(
            "KNOWLEDGE_IMPORT_CLASSIFY_INCOMPLETE",
            "部分来源尚未分类，修复客户端后可续做。",
            {"run_id": str(run), "failures": failures},
        )
