# 使用管理员核验的 Python 基础镜像构建独立 Java 环境，不修改已有浏览器沙箱。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN timeout --kill-after=5s 300s apt-get update \
    && timeout --kill-after=5s 600s apt-get install -y --no-install-recommends git ca-certificates openjdk-17-jdk-headless maven \
    && java -version && mvn --version \
    && groupadd --gid 10001 weora && useradd --uid 10001 --gid 10001 --no-create-home weora \
    && apt-get clean && rm -r /var/lib/apt/lists/*
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=50 -XX:ActiveProcessorCount=1"
USER 10001:10001
WORKDIR /workspace
# 执行期仍由 DockerSandbox 强制无网络、只读根文件系统和资源限制，不缓存个人 Maven 凭据。
