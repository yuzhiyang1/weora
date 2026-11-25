"""保留代码索引回执，并对同一版证据补齐导入后的设计文档。"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from weora_cli.collab_client import CollabError, as_object, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_classifier import code_design_driver, configured_code_design_driver

type ImportDesignDriver = str | None | Literal[False]
type GenerateDesign = Callable[[dict[str, JsonValue]], dict[str, JsonValue]]


def select_import_design_driver(selected: str | None, *, skip: bool) -> ImportDesignDriver:
    if skip:
        return False
    configured = configured_code_design_driver(selected)
    if configured:
        # 明确指定但无法运行的客户端在发布代码证据之前报错。
        return code_design_driver(configured)
    try:
        return code_design_driver(None)
    except CollabError as error:
        if error.code != "KNOWLEDGE_CODE_ENRICH_TOOL":
            raise
        # 机器还没有可用客户端。先保存证据，安装后可以补齐设计。
        return None


def saved_design_driver(state: dict[str, JsonValue]) -> ImportDesignDriver:
    value = state.get("design_driver", False)
    return value if isinstance(value, str) or value is None else False


def indexed_for_design(row: dict[str, JsonValue]) -> bool:
    return row.get("status") == "indexed" or (row.get("status") == "failed" and row.get("phase") == "design")


def _empty_design(index: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    files = index.get("fileCount", index.get("indexed"))
    if type(files) is int and files == 0:
        return {"complete": True, "skipped": "no_components"}
    return None


def finish_import_design(
    index: dict[str, JsonValue], generate: GenerateDesign, retry: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    try:
        design = _empty_design(index) or generate(index)
    except CollabError as error:
        raise CollabError(
            "KNOWLEDGE_CODE_IMPORT_DESIGN_INCOMPLETE", "代码索引已发布，重新运行深度生成可补齐设计文档。",
            {"index": index, "design": {"code": error.code, "result": error.data}, "retry": retry},
        ) from error
    return index | {"design": design}


def finish_batch_designs(
    state_path: Path, state: dict[str, JsonValue], rows: dict[str, JsonValue],
    workers: int, generate: GenerateDesign,
) -> None:
    pending = [
        (key, row) for key, value in rows.items() if indexed_for_design(row := as_object(value))
    ]
    if not pending:
        return
    def design_for(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _empty_design(as_object(row["result"])) or generate(row)

    with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as executor:
        jobs = {executor.submit(design_for, row): (key, row) for key, row in pending}
        for future in as_completed(jobs):
            key, row = jobs[future]
            result = {field: value for field, value in row.items() if field not in {"phase", "code", "design"}}
            try:
                result |= {"status": "succeeded", "design": future.result()}
            except CollabError as error:
                result |= {"status": "failed", "phase": "design", "code": error.code, "design": error.data}
            # 由主线程逐项保存回执，其他成功仓库不受单个 Agent 失败影响。
            rows[key] = result
            write_json(state_path, state)
