FROM node:22-alpine AS builder

WORKDIR /workspace

# 根目录的 packageManager 字段固定 pnpm 版本，避免锁文件格式漂移。
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
RUN pnpm install --frozen-lockfile --filter @weora/web...

COPY apps/web ./apps/web
RUN pnpm --filter @weora/web build

FROM nginx:1.27-alpine

COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /workspace/apps/web/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
