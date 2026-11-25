# Weora Backend

Weora 后端基于 Python 3.12、FastAPI、SQLAlchemy、PostgreSQL、Redis 和 Celery 构建。

当前目录包含可运行的应用骨架。业务模块会在真实功能进入开发时，按照根目录 `AGENTS.md` 中的领域边界逐步建立。

常用命令见 [`docs/development/getting-started.md`](../docs/development/getting-started.md)。

知识库词级检索使用 ICU。Debian 后端镜像包含 `libicu72`，Windows 开发环境使用系统 `icu.dll`。
同一数据库的迁移、API 和执行进程使用相同版本的 ICU。运行 `20261119_0044` 前先停止旧版本的知识库读写进程，
完成词项回填后再启动新版。回退时按相同顺序切换应用与迁移版本。
