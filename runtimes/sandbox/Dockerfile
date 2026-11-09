# 基础镜像必须由管理员提供已核验的完整 sha256 镜像 ID 或仓库@sha256 引用；不使用隐式 latest。
ARG NODE_IMAGE
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS python-runtime

ARG PYTEST_VERSION=9.1.1
LABEL weora.managed="v1" weora.purpose="sandbox-runtime"
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/weora-browsers \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 分层保留已核验的安装结果；网络失败不要求重复获取此前阶段的依赖。
RUN timeout --kill-after=5s 300s apt-get update
RUN timeout --kill-after=5s 600s apt-get install -y --no-install-recommends git ca-certificates
RUN python3 -c "import sys; assert sys.version_info[:2] == (3,12)" \
    && timeout --kill-after=5s 180s python3 -m pip install --no-cache-dir "pytest==${PYTEST_VERSION}"

FROM ${NODE_IMAGE} AS node-runtime
FROM python-runtime
ARG PLAYWRIGHT_VERSION=1.64.1
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/ /usr/local/lib/node_modules/

# 依赖只在构建阶段安装。运行时无网络，不向容器传递任何企业凭据。
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && timeout --kill-after=5s 180s npm install --prefix /opt/weora-browser --no-save --ignore-scripts --no-audit --no-fund "playwright@${PLAYWRIGHT_VERSION}"
# 可选缓存只接受已按 apt 签名索引核对 SHA256 的公开 .deb，默认目录为空。
COPY build-cache/apt/ /var/cache/apt/archives/
# 官方 slim 的自动清理 hook 会在 update 后删除预校验缓存；只在构建时暂存，最终显式清理。
RUN mv /etc/apt/apt.conf.d/docker-clean /tmp/weora-docker-clean \
    && timeout --kill-after=5s 900s node /opt/weora-browser/node_modules/playwright/cli.js install-deps chromium \
    && mv /tmp/weora-docker-clean /etc/apt/apt.conf.d/docker-clean
# 平台仅提供 headless 验证，不额外下载未使用的 headed 浏览器。
COPY build-cache/browser/ /opt/weora-browser-cache/
COPY install_browser_cache.py /opt/weora-install-browser-cache.py
RUN timeout --kill-after=5s 600s python3 /opt/weora-install-browser-cache.py \
    && rm -r /opt/weora-browser-cache \
    && rm /opt/weora-install-browser-cache.py
RUN chmod -R a+rX /opt/weora-browser /opt/weora-browsers \
    && groupadd --gid 10001 weora \
    && useradd --uid 10001 --gid 10001 --no-create-home weora \
    && apt-get clean

# 临时工作区、资源、网络和进程边界由 DockerSandbox 在每次创建时强制传入。
USER 10001:10001
WORKDIR /workspace
CMD ["python3", "-c", "import time; time.sleep(86400)"]
