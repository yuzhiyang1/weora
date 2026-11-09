"""ASGI 进程入口。"""

from weora.bootstrap.application import create_app

app = create_app()
