ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG CODEX_VERSION
ARG CLAUDE_CODE_VERSION

# 两个客户端由 CI 指定版本，运行时由 CLI 固定本地镜像 ID 或仓库摘要。
RUN test -n "${CODEX_VERSION}" && test -n "${CLAUDE_CODE_VERSION}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 \
    && npm install --global "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

USER 1000:1000
