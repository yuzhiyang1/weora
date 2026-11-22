"""覆盖 Python 调用的别名、作用域和跨文件目标校验。"""

from weora_code_graph.extractor import extract, resolved_calls


def test_calls_resolve_declared_module_symbols_without_counting_shadowed_calls() -> None:
    source = """from .billing import charge as debit, missing, nested, mutable
from . import billing as rules
import shop.billing
import shop.billing as module
from .billing import charge as ambiguous
if False:
    from .refunds import charge as ambiguous

def run():
    debit()
    rules.charge()
    shop.billing.charge()
    module.Receipt()
    missing()
    nested()
    mutable()
    ambiguous()
    return [debit() for debit in ()]

def use_parameter(debit):
    debit()

def use_local():
    module = object()
    module.Receipt()

def rebind_global():
    global mutable
    mutable = None

def use_local_import():
    from .billing import charge as local_charge
    local_charge()
"""
    files = {
        "src/shop/entry.py": source,
        "src/shop/billing.py": (
            "async def charge(): pass\nclass Receipt: pass\n"
            "def mutable(): pass\n"
            "def internal():\n    def nested(): pass\n"
        ),
        "src/shop/refunds.py": "def charge(): pass\n",
    }
    manifest = {path: extract(path, "oid", content) for path, content in files.items()}
    calls = resolved_calls("src/shop/entry.py", manifest, set(files))
    assert [(call["callee"], call["symbol"], call["line"]) for call in calls] == [
        ("debit", "charge", 10),
        ("rules.charge", "charge", 11),
        ("shop.billing.charge", "charge", 12),
        ("module.Receipt", "Receipt", 13),
        ("local_charge", "charge", 33),
    ]
    assert {call["target"] for call in calls} == {"src/shop/billing.py"}


def test_same_file_and_ambiguous_packages_do_not_produce_links() -> None:
    files = {
        "entry.py": "from . import schedule\nfrom pricing import quote\nschedule()\nquote()\n",
        "__init__.py": "def schedule(): pass\n",
        "pricing.py": "def quote(): pass\n",
        "src/pricing.py": "def quote(): pass\n",
    }
    manifest = {path: extract(path, "oid", content) for path, content in files.items()}
    assert resolved_calls("entry.py", manifest, set(files)) == [
        {"module": ".", "symbol": "schedule", "callee": "schedule", "line": 3, "target": "__init__.py"},
    ]
    assert not resolved_calls("__init__.py", manifest, set(files))
