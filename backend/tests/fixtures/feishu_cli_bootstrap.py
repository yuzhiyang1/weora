"""独立验证读取装配，不启动服务、不连接数据库或飞书。"""

import asyncio
import sys

from cryptography.fernet import Fernet

from weora.bootstrap.execution_services import ManagedFeishuReader
from weora.modules.context.feishu import FeishuHttpReader, FernetSecrets
from weora.modules.context.feishu_cli import FeishuCliProcess, FeishuCliReader


async def main() -> None:
    cipher = FernetSecrets(Fernet.generate_key().decode())
    for process, expected in ((FeishuCliProcess(sys.executable), FeishuCliReader), (None, FeishuHttpReader)):
        reader = ManagedFeishuReader(cipher, process)
        try:
            assert type(reader._reader()) is expected
        finally:
            await reader.aclose()


asyncio.run(main())
