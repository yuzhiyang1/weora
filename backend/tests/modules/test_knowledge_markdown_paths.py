"""覆盖正文链接的路径规则和不会参与关系计算的 Markdown 内容。"""

from weora.modules.knowledge.markdown_paths_v1 import indexed_paths


def test_markdown_paths_canonicalize_links_and_exclude_non_links() -> None:
    markdown = """
[目录别名](../../skills/release/SKILL.md)
[经验](learnings/failure.md?history=1)
[Wiki](teamwiki/evidence/code/service/entry)
[带括号的路径](a(b).md)
[外部](https://example.com/docs/ignore.md) [绝对](/docs/server.md) [同页](#here)
[越界](../../../docs/escaped.md) [自己](current.md) ![图片](photo.jpg)

~~~md
[围栏](fenced.md)
~~~

    [缩进](indented.md)

[ref][guide]

[guide]: /docs/skip.md
[reserved]: rules/check.md "未在正文使用"
"""
    markdown += "\n" + chr(96) + "[内联](ignored.md)" + chr(96)
    paths = [path for path, _ in indexed_paths("doc", "docs/sub/current.md", markdown)]
    assert paths == [
        "docs/sub/current.md", "skills/release", "learnings/failure", "teamwiki/evidence/code/service/entry.md",
        "docs/sub/a(b).md", "rules/check.md",
    ]
    assert [path for path, _ in indexed_paths("skill", "skills/release", markdown)] == ["skills/release"]
