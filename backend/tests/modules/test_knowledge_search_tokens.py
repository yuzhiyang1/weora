"""记录 TeamAI 词级契约，回归检测 ICU 字典与 UTF-16 边界。"""

import pytest

from weora.modules.knowledge.search_tokens_v2 import compile_query


@pytest.mark.parametrize(("query", "terms", "words"), [
    ("推理服务崩溃", ("推理", "服务", "崩溃"), ("推理", "服务", "崩溃")),
    ("中间件排查", ("中间", "件", "排", "查", "件排", "排查"), ("中间件", "排查")),
    ("连接池耗尽", ("连接", "池", "耗", "尽", "池耗", "耗尽"), ("连接池", "耗尽")),
    ("客户AppID错误", ("客户", "appid", "app", "id", "错误"), ("客户", "AppID", "错误")),
    ("🐈 中文１２.３", ("中文", "１２.３"), ("中文", "１２.３")),
    ("如何检查错误率", ("检查", "错误", "率"), ("如何", "检查", "错误率")),
])
def test_recall_segmentation(query: str, terms: tuple[str, ...], words: tuple[str, ...]) -> None:
    compiled = compile_query(query)
    assert compiled.indexed == terms
    assert tuple(word for word, _ in compiled.words) == words
    assert compile_query(query, with_coverage=False).words == ()
