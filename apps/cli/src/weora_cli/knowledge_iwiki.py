"""通过 iWiki MCP 逐页读取授权资料，记录失败分支和可重试的页面。"""

import json
import os
import re
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from http.client import HTTPException, HTTPResponse, IncompleteRead
from itertools import count
from typing import BinaryIO, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from weora_code_graph.iwiki_links import key_terms

from weora_cli.collab_client import CollabError, _NoRedirect
from weora_cli.commands import JsonValue

_ENDPOINT = "https://prod.mcp.it.woa.com/app_iwiki_mcp/mcp3"
_HOST = "iwiki.woa.com"
_PAGE_LIMIT = 200
_PAGE_BATCH = 5
_RESPONSE_LIMIT = 4 * 1024**2
_DOCUMENT_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_REQUESTS = count(1)


def prepare_fetch(source: str) -> dict[str, JsonValue]:
    """固定来源选择，之后的续做不再解释命令行参数。"""
    source = source.strip()
    if re.fullmatch(r"[0-9]{1,128}", source):
        return {"trees": [source], "pages": [], "seen": [source], "truncated": False}
    try:
        url = urlsplit(source)
        match = re.fullmatch(r"/(?:p|pages)/([a-zA-Z0-9_-]{1,128})/?", url.path)
        if url.scheme == "https" and url.netloc.lower() == _HOST and match:
            document = match[1]
            return {"trees": [], "pages": [document], "seen": [document], "truncated": False}
    except ValueError:
        pass
    raise CollabError("KNOWLEDGE_IWIKI_INVALID_TARGET", "请输入数字空间 ID 或 iWiki 页面链接。")


def _invalid_response() -> CollabError:
    return CollabError("KNOWLEDGE_IWIKI_INVALID_RESPONSE", "iWiki 返回了无法读取的响应。")


def _envelope(body: bytes, request_id: int) -> JsonValue:
    """兼容 JSON 和流式事件，始终按请求 ID 选择响应。"""
    try:
        text = body.decode("utf-8-sig")
        messages: list[JsonValue]
        if text.lstrip().startswith("{"):
            messages = [json.loads(text)]
        else:
            messages = []
            for event in re.split(r"\r?\n\r?\n", text):
                payload = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                if payload:
                    messages.append(json.loads(payload))
        for message in messages:
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise CollabError("KNOWLEDGE_IWIKI_TOOL_FAILED", "iWiki 无法读取此来源。")
            result = message.get("result")
            if isinstance(result, dict):
                if result.get("isError") is True:
                    raise CollabError("KNOWLEDGE_IWIKI_TOOL_FAILED", "iWiki 无法读取此来源。")
                if "structuredContent" in result:
                    return result["structuredContent"]
                content = result.get("content")
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    value = content[0].get("text")
                    if isinstance(value, str):
                        try:
                            parsed: JsonValue = json.loads(value)
                            return parsed
                        except ValueError:
                            return value
            if "result" in message:
                return result
    except (UnicodeError, ValueError) as exc:
        raise _invalid_response() from exc
    raise _invalid_response()


def _stream(response: BinaryIO, request_id: int) -> JsonValue:
    """收到匹配的事件即返回，即使服务端仍保持流连接。"""
    consumed = 0
    fragments: list[bytes] = []
    while consumed <= _RESPONSE_LIMIT:
        line = response.readline(_RESPONSE_LIMIT - consumed + 1)
        consumed += len(line)
        if consumed > _RESPONSE_LIMIT:
            break
        if line.startswith(b"data:"):
            fragments.append(line[5:].strip())
        if fragments and not line.strip():
            data = b"\n".join(fragments)
            fragments.clear()
            try:
                event: JsonValue = json.loads(data)
            except (UnicodeError, ValueError) as exc:
                raise _invalid_response() from exc
            if isinstance(event, dict) and event.get("id") == request_id:
                return _envelope(data, request_id)
        if not line:
            raise _invalid_response()
    raise CollabError("KNOWLEDGE_IWIKI_RESPONSE_TOO_LARGE", "iWiki 响应超过大小限制。")


def _call(
    token: str,
    tool: Literal["getSpacePageTree", "getDocument", "metadata"],
    arguments: dict[str, str],
) -> JsonValue:
    # 输入链接只用于提取 ID。PAT 始终发给同一 iWiki 端点，重定向直接拒绝。
    request_id = next(_REQUESTS)
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    request = Request(
        _ENDPOINT,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            if response.headers.get_content_type() == "text/event-stream":
                return _stream(response, request_id)
            body = response.read(_RESPONSE_LIMIT + 1)
            # 有界读取在固定长度响应提前断开时，可能只留下正数的剩余长度。
            if len(body) <= _RESPONSE_LIMIT and isinstance(response, HTTPResponse) and response.length:
                raise IncompleteRead(b"", response.length)
    except HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise CollabError("KNOWLEDGE_IWIKI_ACCESS_DENIED", "iWiki 凭据或来源访问权限无效。") from exc
        raise CollabError("KNOWLEDGE_IWIKI_HTTP_FAILED", f"iWiki 请求失败（HTTP {exc.code}）。") from exc
    except (HTTPException, URLError, OSError, TimeoutError) as exc:
        raise CollabError("KNOWLEDGE_IWIKI_UNREACHABLE", "暂时无法连接 iWiki。") from exc
    if len(body) > _RESPONSE_LIMIT:
        raise CollabError("KNOWLEDGE_IWIKI_RESPONSE_TOO_LARGE", "iWiki 响应超过大小限制。")
    return _envelope(body, request_id)


def _children(token: str, parent: str) -> list[tuple[str, bool]]:
    result = _call(token, "getSpacePageTree", {"parentid": parent})
    if not isinstance(result, list):
        raise _invalid_response()
    children = []
    for value in result:
        if not isinstance(value, dict):
            raise _invalid_response()
        document = value.get("docid") or value.get("id")
        if type(document) not in (int, str) or not _DOCUMENT_ID.fullmatch(str(document)):
            raise _invalid_response()
        children.append((str(document), bool(value.get("has_children"))))
    return children


def _document_content(token: str, document: str, *, dual: bool) -> JsonValue:
    result = _call(token, "getDocument", {"docid": document})
    body = result.get("content") if isinstance(result, dict) else result
    if not isinstance(body, str):
        raise _invalid_response()
    if not body.strip():
        return None
    candidate: dict[str, JsonValue] = {"excerpt": body[:3000]}
    if terms := key_terms(body):
        candidate["iwiki_terms"] = list(terms)
    if dual:
        # 汇总使用团队导入同一批原页，每页最多保留五千字符。
        candidate["dual_excerpt"] = body[:5000]
    return candidate


def _document_title(token: str, document: str) -> JsonValue:
    metadata = _call(token, "metadata", {"docid": document})
    if not isinstance(metadata, dict):
        raise _invalid_response()
    title = metadata.get("title")
    return title[:160] if isinstance(title, str) and title.strip() else None


def _ids(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and _DOCUMENT_ID.fullmatch(item) for item in value):
        raise CollabError("KNOWLEDGE_IWIKI_PROGRESS_INVALID", "本机的 iWiki 导入记录无效。")
    return [str(item) for item in value]


def _partial_downloads(progress: dict[str, JsonValue], pending: set[str]) -> dict[str, dict[str, JsonValue]]:
    """旧检查点没有下载分片，按待下载页面重新开始。"""
    saved = progress.get("downloads", {})
    invalid = CollabError("KNOWLEDGE_IWIKI_PROGRESS_INVALID", "本机的 iWiki 下载记录无效。")
    if not isinstance(saved, dict):
        raise invalid
    downloads = {}
    for page, parts in saved.items():
        if page not in pending or not isinstance(parts, dict) or parts.keys() - {"content", "title_hint"}:
            raise invalid
        body = parts.get("content")
        if body is not None and (
            not isinstance(body, dict) or body.keys() - {"excerpt", "iwiki_terms", "dual_excerpt"}
            or not isinstance(body.get("excerpt"), str)
        ):
            raise invalid
        if not isinstance(parts.get("title_hint"), (str, type(None))):
            raise invalid
        downloads[page] = parts
    return downloads


def fetch_pages(
    progress: dict[str, JsonValue],
    candidates: list[JsonValue],
    checkpoint: Callable[[], None],
) -> list[JsonValue]:
    """扫描树和下载相互独立。已完成的工作保留，下一次只重试标错项。"""
    trees, pages, seen = _ids(progress["trees"]), _ids(progress["pages"]), _ids(progress["seen"])
    if not trees and not pages:
        return []
    downloads = _partial_downloads(progress, set(pages))
    token = os.environ.get("TAI_PAT_TOKEN", "").strip()
    if trees or any(len(downloads.get(page, {})) < 2 for page in pages):
        if not token:
            raise CollabError("KNOWLEDGE_IWIKI_TOKEN_REQUIRED", "请为此命令设置环境变量 TAI_PAT_TOKEN。")
        if any(character.isspace() for character in token):
            raise CollabError("KNOWLEDGE_IWIKI_TOKEN_INVALID", "TAI_PAT_TOKEN 的格式无效。")
    failures: list[JsonValue] = []
    pending_trees = trees.copy()
    discovered = set(seen)

    def save() -> None:
        progress.update(trees=list(pending_trees), pages=list(pages), seen=list(seen))
        progress["downloads"] = {page: parts for page, parts in downloads.items()}
        checkpoint()

    with ThreadPoolExecutor(max_workers=2 * _PAGE_BATCH) as workers:
        while trees and not progress["truncated"]:
            batch, trees = trees[:_PAGE_BATCH], trees[_PAGE_BATCH:]
            tree_jobs = {parent: workers.submit(_children, token, parent) for parent in batch}
            for parent, tree_job in tree_jobs.items():
                try:
                    children = tree_job.result()
                except CollabError as exc:
                    failures.append({"stage": "tree", "page": parent, "code": exc.code})
                    continue
                pending_trees.remove(parent)
                for document, branch in children:
                    if document in discovered:
                        continue
                    if len(seen) - 1 >= _PAGE_LIMIT:
                        progress["truncated"] = True
                        break
                    seen.append(document)
                    discovered.add(document)
                    pages.append(document)
                    if branch:
                        trees.append(document)
                        pending_trees.append(document)
                save()
        if progress["truncated"]:
            pending_trees.clear()
            failures.clear()
            save()
            print(f"空间导入达到 {_PAGE_LIMIT} 页上限，可另用页面链接导入其余资料。", file=sys.stderr)
        to_download = pages.copy()
        dual = progress.get("dual") is True
        for offset in range(0, len(to_download), _PAGE_BATCH):
            batch = to_download[offset : offset + _PAGE_BATCH]
            jobs: dict[Future[JsonValue], tuple[str, str]] = {}
            for page in batch:
                parts = downloads.get(page, {})
                if "content" not in parts:
                    jobs[workers.submit(_document_content, token, page, dual=dual)] = page, "content"
                if "title_hint" not in parts:
                    jobs[workers.submit(_document_title, token, page)] = page, "title_hint"
            errors: dict[str, str] = {}
            for job in as_completed(jobs):
                page, field = jobs[job]
                try:
                    value = job.result()
                except CollabError as exc:
                    if field == "content" or page not in errors:
                        errors[page] = exc.code
                else:
                    # 两个读取结果各自落检查点，续做只请求缺失的部分。
                    downloads.setdefault(page, {})[field] = value
                    save()
            for page in batch:
                if page in errors:
                    failures.append({"stage": "download", "page": page, "code": errors[page]})
                    continue
                parts = downloads.pop(page)
                pages.remove(page)
                if isinstance(content := parts.get("content"), dict):
                    candidates.append(content | {
                        "ref": f"https://{_HOST}/p/{page}", "source_type": "wiki", "title_hint": parts["title_hint"],
                    })
            save()
    return failures
