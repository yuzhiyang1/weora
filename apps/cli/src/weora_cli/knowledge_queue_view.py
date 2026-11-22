"""为当前设备召回本机经验，平台只接收用于合并的标识。"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from weora_cli.commands import JsonValue


def learning_path(value: str) -> str | None:
    normalized = PurePosixPath(value.strip().replace("\\", "/"))
    if not normalized.parts or normalized.is_absolute() or ".." in normalized.parts or ":" in value:
        return None
    path = normalized.as_posix()
    if len(path) > 1000 or not path.isprintable():
        return None
    return path[:-3] if path.startswith("learnings/") and path.lower().endswith(".md") else path


@dataclass(frozen=True, slots=True)
class QueuedItem:
    item: dict[str, JsonValue]
    path: str
    matches: bool
    matched_terms: tuple[str, ...]
    missing_terms: tuple[str, ...]

    def hint(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.item["id"], "project_id": self.item["project_id"],
            "path": self.path, "matches": self.matches,
        }


    def check_details(self, *, offline: bool) -> dict[str, JsonValue]:
        """本机队列只有词项命中，不使用平台的相关性分数。"""
        title = self.item.get("title")
        return {
            "relevant": True, "score": None, "threshold": None, "reason": "local-queue",
            "title": title if isinstance(title, str) else self.path,
            "entry_id": self.item["id"], "resource_path": self.path,
            "matched_terms": list(self.matched_terms), "missing_terms": list(self.missing_terms),
            "sources": [], "offline": offline,
        }


def queue_view(
    items: list[dict[str, JsonValue]], query: str, *, recall: bool, path: str | None = None,
) -> list[QueuedItem]:
    result: list[QueuedItem] = []
    terms = tuple(dict.fromkeys(query.split()))
    selected_path = learning_path(path) if path is not None else None
    for item in items:
        source = item.get("source")
        if not isinstance(source, dict):
            continue
        try:
            identifier = str(UUID(str(item["id"])))
        except (KeyError, ValueError):
            continue
        namespace = source.get("namespace")
        explicit = source.get("resource_path")
        directory = f"learnings/{namespace}" if isinstance(namespace, str) and namespace else "learnings"
        target = learning_path(explicit if isinstance(explicit, str) else f"{directory}/{identifier}")
        if target is None or (path is not None and target != selected_path):
            continue
        tags = item.get("tags")
        tag_text = " ".join(tag for tag in tags if isinstance(tag, str)) if isinstance(tags, list) else ""
        text = f"{item.get('title', '')} {tag_text}"
        if not recall:
            text += f" {item.get('body', '')}"
        candidate_text = text.casefold()
        matched = tuple(term for term in terms if term.casefold() in candidate_text)
        missing = tuple(term for term in terms if term not in matched)
        result.append(QueuedItem(item, target, path is not None or bool(matched), matched, missing))
    return result[:100]


def queue_matches(rows: list[QueuedItem], *, visible: JsonValue = None) -> list[dict[str, JsonValue]]:
    if isinstance(visible, list):
        matched = {str(row.item["id"]): row.item for row in rows if row.matches}
        return [matched[item] for item in visible if isinstance(item, str) and item in matched]
    winners: dict[str, QueuedItem] = {}
    for row in sorted(rows, key=lambda item: item.item["project_id"] is None):
        winners.setdefault(row.path, row)
    return [row.item for row in winners.values() if row.matches]
