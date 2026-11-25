"""渲染生成式评分标准中允许的变量。"""

import re

from weora.modules.knowledge.models import ScorerLabel

_TEMPLATE = re.compile(r"(?<!\$)\{\{\s*(?:doc|skill|task|inputs|steps|with|run)\.[^{}]*?(?:\}\}|$)")


def fill_template(
    prompt: str, labels: list[ScorerLabel], values: dict[str, str],
) -> tuple[str, list[ScorerLabel]]:
    def expand(text: str) -> str:
        # 对解析后的字段插值，文档路径中的引号不会改变标签或 YAML 结构。
        def replace(match: re.Match[str]) -> str:
            token = match[0]
            name = token[2:-2].strip()
            if not token.endswith("}}") or name not in values:
                raise ValueError("评分模板含有不支持的变量。")
            return values[name]

        # 保留提示词例子里的 GitHub Actions 插值和显式转义的大括号。
        return "{{".join(_TEMPLATE.sub(replace, fragment) for fragment in text.split(r"\{{"))

    body = expand(prompt)
    rendered: list[ScorerLabel] = [
        {"value": expand(label["value"]).strip(), "description": expand(label["description"]), "score": label["score"]}
        for label in labels
    ]
    if len(body) > 24_000 or any(
        not 1 <= len(label["value"]) <= 80 or len(label["description"]) > 2000 for label in rendered
    ) or len({label["value"] for label in rendered}) != len(rendered):
        raise ValueError("展开后的评分正文或标签超出长度限制，或包含重复标签。")
    return body, rendered
