FROM ghcr.io/astral-sh/uv:0.7.12-python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 知识索引回填与在线检索使用同一 ICU 中文词典。
RUN apt-get update && apt-get install -y --no-install-recommends libicu72 \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖元数据，使业务代码变化时仍能复用依赖缓存层。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "weora.main:app", "--host", "0.0.0.0", "--port", "8000"]
