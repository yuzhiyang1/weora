# rootfs/ 只放审计后的缓存、无凭据设置和清单；文件须预先设为 root 所有、0444/0555。
# 合并为一次 COPY，避免大依赖层之后逐项复制与修改权限造成额外快照开销。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SOURCE_REVISION
LABEL weora.maven.source-revision=${SOURCE_REVISION}
ENV MAVEN_OPTS="-Xmx768m"
# 继承受信任 Java 基础镜像的非 root 用户及 /workspace 工作目录，不再创建后置层。
COPY rootfs/ /
