# Build a derived image for each managed project sandbox that needs native tools.
ARG SANDBOX_IMAGE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS node-runtime
FROM ${SANDBOX_IMAGE}
USER root
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/ /usr/local/lib/node_modules/
WORKDIR /opt/weora-coding-tools
COPY package.json package-lock.json ./
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && timeout --kill-after=5s 300s apt-get update \
    && timeout --kill-after=5s 600s apt-get install -y --no-install-recommends ripgrep fd-find \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd && rm -rf /var/lib/apt/lists/* \
    && timeout --kill-after=5s 900s npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && arch="$(node -p "process.arch === 'arm64' ? 'aarch64' : process.arch")" \
    && ln -s "/opt/weora-coding-tools/node_modules/@oven/bun-linux-$arch/bin/bun" /usr/local/bin/weora-bun \
    && node --version && weora-bun --version
COPY dispatch.mjs smoke.mjs ./
COPY weora-native-tool /usr/local/bin/weora-native-tool
RUN chmod 0755 /usr/local/bin/weora-native-tool
USER 10001:10001
WORKDIR /workspace
