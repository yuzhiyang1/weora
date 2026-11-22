"""第一版知识检索分词。历史迁移固定使用本模块的词条格式。"""

import re
from collections import Counter
from collections.abc import Iterable
from unicodedata import normalize

TITLE, TAG, TEXT = 1, 2, 4
_CJK = r"\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af"
_WORDS = re.compile(rf"[{_CJK}]+|(?:(?![{_CJK}])[^\W_])+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_DOMAINS = {
    "technical": {
        "api", "sdk", "rpc", "code", "architecture", "design", "database", "sql", "algorithm", "protocol",
        "implementation", "performance", "test", "testing", "debug", "typescript", "python", "java",
        "react", "postgres", "redis", "接口", "架构", "设计", "算法", "协议", "实现", "性能", "测试", "调试",
        "代码", "并发", "事务", "缓存",
    },
    "ops": {
        "k8s", "kubernetes", "deploy", "deployment", "cluster", "node", "pod", "sop", "upgrade", "rollout",
        "rollback", "restart", "scale", "monitor", "alert", "metrics", "grafana", "prometheus", "log",
        "pipeline", "ci", "cd", "cicd", "release", "publish", "nginx", "ingress", "network", "firewall",
        "backup", "restore", "incident", "oncall", "容器", "部署", "发布", "上线", "回滚", "扩容", "缩容",
        "重启", "集群", "节点", "监控", "告警", "指标", "日志", "排查", "故障", "值班", "网关", "流量", "备份",
        "恢复", "容灾", "配额", "权限", "证书",
    },
    "support": {
        "faq", "support", "user", "customer", "guide", "tutorial", "onboard", "onboarding", "help", "howto",
        "usage", "example", "feedback", "issue", "complaint", "request", "ticket", "教程", "指南", "示例",
        "用法", "入门", "新手", "帮助", "反馈", "咨询", "问题", "工单", "需求", "客户", "用户",
    },
}
_PATHS = {
    "technical": ("docs/architecture/", "docs/design/", "docs/api/", "docs/adr/"),
    "ops": ("learnings/ops/", "docs/ops/", "docs/deploy/", "docs/sre/"),
    "support": ("docs/support/", "docs/faq/", "docs/guide/", "learnings/support/"),
}


def tokens(text: str) -> Iterable[str]:
    """保留完整标识符及驼峰词，连续中日韩字符按相邻两字索引。"""
    for match in _WORDS.finditer(normalize("NFKC", text)):
        word = match[0]
        if re.fullmatch(rf"[{_CJK}]+", word):
            yield from (word[index:index + 2] for index in range(max(1, len(word) - 1)))
        elif len(word) <= 80:
            parts = _CAMEL.split(word)
            for term in dict.fromkeys(part.casefold() for part in [word, *parts]):
                if len(term) <= 160:
                    yield term


def infer_domain(terms: Iterable[str], path: str = "") -> str:
    vocabulary = set(terms)
    path = "/" + path.lower().replace("\\", "/")
    for domain, patterns in _PATHS.items():
        if any(f"/{pattern}" in path for pattern in patterns):
            return domain
    counts = {domain: len(vocabulary & words) for domain, words in _DOMAINS.items()}
    return max(counts, key=counts.__getitem__) if any(counts.values()) else "neutral"


def document_index(
    title: str, body: str, tags: list[str], source: dict[str, str],
) -> tuple[str, int, int, dict[str, tuple[int, int]]]:
    """返回领域、标题长度、文本长度和词项字段及出现频率。"""
    title_terms, tag_terms = set(tokens(title)), set(tokens(" ".join(tags)))
    frequency = Counter(tokens(body))
    frequency.update(title_terms)
    frequency.update(tag_terms)
    postings = {
        term: (
            TEXT | (TITLE if term in title_terms else 0) | (TAG if term in tag_terms else 0),
            count,
        )
        for term, count in frequency.items()
    }
    return infer_domain(tag_terms, source.get("path", "")), len(title_terms), sum(frequency.values()), postings
