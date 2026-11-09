"""测试运行时的公共配置。"""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """当前后端统一使用 asyncio，避免测试套件重复执行其他异步后端。"""
    return "asyncio"
