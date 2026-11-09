FROM oven/bun:1.3.14-alpine
WORKDIR /app
COPY supervisor/package.json supervisor/bun.lock ./
RUN bun install --frozen-lockfile
COPY supervisor/src ./src
COPY LICENSE ./LICENSE.openbot
EXPOSE 4300
CMD ["bun", "src/index.ts"]
