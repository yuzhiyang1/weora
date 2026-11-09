#!/usr/bin/env bash
# 构建只取固定版本的两个服务，补丁应用于临时目录。
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
revision=b6932d31a8d6e7896c15139dfc27a6c6911deb27
context="$(mktemp -d /tmp/weora-openbot.XXXXXXXX)"
trap 'rm -rf -- "$context"' EXIT

git -C "$context" init -q
git -C "$context" fetch --depth=1 "${WEORA_OPENBOT_SOURCE:-https://github.com/CopilotKit/OpenBot.git}" "$revision"
git -C "$context" checkout -q --detach FETCH_HEAD
test "$(git -C "$context" rev-parse HEAD)" = "$revision"
# 只把两个电脑组件及许可证送给 Docker，避免把上游整库放入构建上下文。
images="$context/images"
mkdir "$images"
cp -a "$context/agent-computer" "$context/supervisor" "$context/LICENSE" "$images/"
cp "$root/desktop.ts" "$root/evidence.ts" "$images/agent-computer/src/"
python3 "$root/patch.py" "$images" "${WEORA_OPENBOT_CHROMIUM_ARCHIVE:-}"
cp "$root/supervisor.Dockerfile" "$images/weora-supervisor.Dockerfile"
# 可选的 Docker Hub 公共镜像缓存保留上游镜像版本和摘要。
if [[ "${WEORA_OPENBOT_DOCKERHUB_MIRROR:-}" == "mirror.gcr.io" ]]; then
  sed -i -E 's#^FROM (node:|ubuntu:)#FROM mirror.gcr.io/library/\1#; s#^FROM oven/#FROM mirror.gcr.io/oven/#' \
    "$images/agent-computer/Dockerfile" "$images/weora-supervisor.Dockerfile"
fi
docker build -f "$images/weora-supervisor.Dockerfile" -t weora-openbot-supervisor:b6932d3 "$images"
docker build -f "$images/agent-computer/Dockerfile" -t weora-openbot-desktop:b6932d3 "$images"
