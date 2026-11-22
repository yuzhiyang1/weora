"""召回领域分类。分类只使用元数据，词项仍由原有索引生成。"""

from collections.abc import Iterable

from weora.modules.knowledge.resource_paths import normalize_resource_path

# 对照 TeamAI 1104f305 的领域词表。迭代顺序决定同分时的优先级。
_VOCABULARY = {
    "technical": {
        "api", "sdk", "typescript", "python", "golang", "rust", "javascript", "bug", "debug", "error", "exception",
        "fix", "patch", "refactor", "architecture", "framework", "database", "db", "cache", "redis", "async",
        "concurrent", "thread", "performance", "latency", "timeout", "http", "grpc", "proto", "json", "schema",
        "migration", "index", "test", "unittest", "e2e", "mock", "lint", "typecheck", "docker", "build", "package",
        "dependency", "import", "module", "接口", "代码", "函数", "类型", "重构", "报错", "异常", "调试", "修复",
        "补丁", "性能", "延迟", "超时", "重试", "并发", "异步", "线程", "缓存", "架构", "框架", "依赖", "模块",
        "编译", "构建", "测试", "单测", "断言", "算法", "协议",
    },
    "ops": {
        "k8s", "kubernetes", "deploy", "deployment", "cluster", "node", "pod", "sop", "upgrade", "rollout",
        "rollback", "restart", "scale", "monitor", "alert", "metrics", "grafana", "prometheus", "log",
        "pipeline", "ci", "cd", "cicd", "release", "publish", "nginx", "lb", "ingress", "service", "network",
        "firewall", "backup", "restore", "disaster", "incident", "oncall", "gpu", "resource", "quota", "tke", "tcr",
        "cos", "部署", "发布", "上线", "回滚", "扩容", "缩容", "重启", "集群", "节点", "监控", "告警", "指标",
        "日志", "排查", "故障", "值班", "预案", "容器", "镜像", "网关", "负载", "流量", "带宽", "磁盘", "内存",
        "显存", "备份", "恢复", "容灾", "资源", "配额", "权限", "证书",
    },
    "support": {
        "faq", "support", "user", "customer", "guide", "tutorial", "onboard", "onboarding", "help", "howto", "usage",
        "example", "feedback", "issue", "complaint", "request", "ticket", "教程", "指南", "示例", "用法", "入门",
        "新手", "帮助", "反馈", "咨询", "问题", "工单", "需求", "客户", "用户",
    },
}
_PATHS = {
    "technical": ("docs/architecture/", "docs/design/", "docs/api/", "docs/adr/"),
    "ops": ("learnings/ops/", "docs/ops/", "docs/deploy/", "docs/sre/"),
    "support": ("docs/support/", "docs/faq/", "docs/guide/", "learnings/support/"),
}


def declared_domain(value: object) -> str | None:
    return value if isinstance(value, str) and (value == "neutral" or value in _VOCABULARY) else None


def query_domain(words: Iterable[str]) -> str:
    tokens = tuple(word.lower() for word in words)
    scores = {domain: sum(token in terms for token in tokens) for domain, terms in _VOCABULARY.items()}
    return max(scores, key=scores.__getitem__) if any(scores.values()) else "neutral"


def resource_domain(tags: list[str], kind: str, source: dict[str, str], path: str | None = None) -> str:
    """优先采用声明、完整标签和团队路径，最后按资源类型推断。"""
    if explicit := declared_domain(source.get("domain")):
        return explicit
    if (tag_domain := query_domain(tags)) != "neutral":
        return tag_domain
    location = path or source.get("path") or source.get("resource_path")
    if not location and source.get("type") in {"file", "wiki"}:
        location = normalize_resource_path(source.get("ref", ""))
    normalized = (location or "").replace("\\", "/").lower()
    for domain, fragments in _PATHS.items():
        if any(fragment in normalized for fragment in fragments):
            return domain
    return "technical" if kind in {"rule", "skill"} else "neutral"
