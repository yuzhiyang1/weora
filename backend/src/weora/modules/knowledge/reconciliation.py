"""将产品调和的关系发布到项目图谱并保留生成证据。"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import batched
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeProductMapping, KnowledgeRevision
from weora_code_graph.reconcile import Mapping, Page, Report, documented_update, reconcile

Published = Select[tuple[KnowledgeEntry, KnowledgeRevision]]


def is_product(path: str) -> bool:
    return path.startswith(("product/", "teamwiki/product/"))


def is_code(source: dict[str, str]) -> bool:
    return bool(source.get("path") or source.get("design_origin") == "generated")


def _page(entry: KnowledgeEntry, revision: KnowledgeRevision, ref: str) -> Page:
    confirmed = frozenset(item["target"] for item in revision.relations if item["kind"] == "MAPS_TO")
    return Page(
        str(entry.id), revision.title, revision.body, ref, str(revision.revision),
        confirmed, documented_update(revision.body) or revision.created_at.date(),
    )


async def build_report(db: AsyncSession, scoped: Published, project: UUID, *, preview: bool) -> Report:
    products, code, resources = [], [], []
    selection = scoped.where(
        KnowledgeEntry.kind.in_(("doc", "rule")),
        KnowledgeEntry.codebase_id.is_(None),
    )
    for entry, revision in (await db.execute(selection.order_by(KnowledgeEntry.id))).tuples():
        path = revision.source.get("resource_path", "")
        page = _page(entry, revision, path)
        if entry.kind == "doc" and entry.project_id == project and is_product(path):
            products.append(page)
        elif preview:
            resources.append(page)
    selection = scoped.where(KnowledgeEntry.project_id == project, KnowledgeEntry.codebase_id.is_not(None))
    for entry, revision in (await db.execute(selection.order_by(KnowledgeEntry.id))).tuples():
        if is_code(revision.source):
            code.append(_page(entry, revision, revision.source["ref"]))
    return reconcile(products, code, resources)


@dataclass(frozen=True, slots=True)
class Evidence:
    product_revision: int
    code_revision: int
    terms: list[dict[str, str | float]]


def _entries(mappings: Sequence[Mapping]) -> dict[tuple[UUID, UUID], Evidence]:
    pairs: dict[tuple[UUID, UUID], Evidence] = {}
    for item in mappings:
        source, target = item["source"], item["target"]
        key = UUID(source["id"]), UUID(target["id"])
        if key not in pairs:
            pairs[key] = Evidence(int(source["revision"]), int(target["revision"]), [])
        pairs[key].terms.append({"term": item["term"], "confidence": item["confidence"]})
    return pairs


async def serialize_publication(db: AsyncSession, project: UUID) -> None:
    # 项目行已由权限上下文持有共享锁，另用事务级锁串行发布同一项目的图谱。
    if db.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(project.bytes[:8], signed=True)
        await db.execute(select(func.pg_advisory_xact_lock(key)))


async def replace_mappings(
    db: AsyncSession, project: UUID, mappings: Sequence[Mapping],
) -> dict[UUID, tuple[int, int]]:
    desired = _entries(mappings)
    stmt = select(KnowledgeProductMapping).where(KnowledgeProductMapping.project_id == project)
    current = {(item.product_id, item.code_id): item for item in await db.scalars(stmt)}
    changes: dict[UUID, list[int]] = defaultdict(lambda: [0, 0])
    for pair, previous in current.items():
        if pair not in desired:
            changes[previous.product_id][1] += 1
            await db.delete(previous)
    for (product, code), evidence in desired.items():
        row = current.get((product, code))
        if row and evidence == Evidence(row.product_revision, row.code_revision, row.terms):
            continue
        changes[product][0] += 1
        if row is None:
            row = KnowledgeProductMapping(project_id=project, product_id=product, code_id=code)
            db.add(row)
        row.product_revision = evidence.product_revision
        row.code_revision = evidence.code_revision
        row.terms = evidence.terms
    return {key: (counts[0], counts[1]) for key, counts in changes.items()}


async def active_mappings(
    db: AsyncSession, scoped: Published, project: UUID, pages: dict[UUID, int],
) -> dict[UUID, list[dict[str, str]]]:
    model = KnowledgeProductMapping
    # 使用已有可见性查询核对两端版本。更换或断开仓库后，旧关系立即退出召回。
    live = scoped.where(KnowledgeEntry.project_id == project).with_only_columns(
        KnowledgeEntry.id.label("entry"), KnowledgeRevision.revision.label("revision"),
        KnowledgeRevision.title.label("title"),
    ).subquery()
    product, code = live.alias("product"), live.alias("code")
    ids = tuple(pages)
    statement = (
        select(model, product.c.title, code.c.title.label("code_title"))
        .join(product, (model.product_id == product.c.entry) & (model.product_revision == product.c.revision))
        .join(code, (model.code_id == code.c.entry) & (model.code_revision == code.c.revision))
        .where(model.project_id == project, or_(model.product_id.in_(ids), model.code_id.in_(ids)))
    )
    relations: dict[UUID, list[dict[str, str]]] = defaultdict(list)
    for row, product_title, code_title in (await db.execute(statement)).tuples():
        strongest = max(row.terms, key=lambda term: float(term["confidence"]))
        evidence = {
            "kind": "MAPS_TO", "derived": "product_reconciliation", "evidence": str(strongest["term"]),
            "source_revision": str(row.product_revision), "target_revision": str(row.code_revision),
            "confidence": str(strongest["confidence"]),
        }
        if pages.get(row.product_id) == row.product_revision:
            relations[row.product_id].append(evidence | {
                "direction": "out", "target": str(row.code_id), "title": code_title,
            })
        if pages.get(row.code_id) == row.code_revision:
            relations[row.code_id].append(evidence | {
                "direction": "in", "target": str(row.product_id), "title": product_title,
            })
    return relations


async def changed_products(
    db: AsyncSession, workspace: UUID, project: UUID, ids: Sequence[UUID],
) -> list[KnowledgeEntry]:
    rows: list[KnowledgeEntry] = []
    for group in batched(ids, 2000):
        rows.extend(await db.scalars(select(KnowledgeEntry).where(
            KnowledgeEntry.id.in_(group), KnowledgeEntry.workspace_id == workspace,
            KnowledgeEntry.project_id == project, KnowledgeEntry.kind == "doc",
        )))
    return rows
