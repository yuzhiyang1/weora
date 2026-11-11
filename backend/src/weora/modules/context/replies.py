"""飞书只展示简短摘要，详细报告由运行记录保存。"""

import re

from weora.modules.context.domain import Json

TASK_ACCEPTANCE_REPLY = "好，我来处理这件事。有结果我会在这里告诉你。"


def brief_reply(text: str) -> str:
    """清理纯文本中的 Markdown 标记，并为遗留长回复提供展示上限。"""
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text.strip())
    text = re.sub(r"(?m)^\s*```[^\n]*$", "", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= 280:
        return text
    excerpt = text[:240]
    end = max(excerpt.rfind(mark) for mark in ("。", "！", "？", "\n"))
    excerpt = excerpt[: end + 1] if end >= 100 else excerpt + "…"
    return excerpt.rstrip() + "\n\n详细过程见 Weora 运行记录。"


def reply_content(text: str, image_keys: tuple[str, ...] = ()) -> Json:
    """文字和图片置于同一原生富文本消息，避免分条发送留下半份结果。"""
    paragraphs = [[{"tag": "text", "text": brief_reply(text)}]]
    paragraphs.extend([[{"tag": "img", "image_key": key}] for key in image_keys])
    return {"zh_cn": {"content": paragraphs}}
