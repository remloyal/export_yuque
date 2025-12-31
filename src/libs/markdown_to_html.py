import re
from pathlib import Path
from typing import Optional


def _rewrite_relative_md_links(markdown_text: str) -> str:
    """将 Markdown 中相对的 .md 链接改为 .html，便于本地 HTML 之间跳转。

    仅处理形如 ](xxx.md) / ](xxx.md#anchor) 的内联链接，且排除 http(s) 和 # 开头。
    """

    def repl(match: re.Match) -> str:
        before = match.group(1)
        target = match.group(2)
        anchor = match.group(3) or ""
        after = match.group(4)
        return f"{before}{target}.html{anchor}{after}"

    pattern = re.compile(r"(\]\()(?!(?:https?://)|#)([^)\s#]+?)\.md(\#[^)\s]+)?(\))", re.IGNORECASE)
    return pattern.sub(repl, markdown_text)


def markdown_to_html(markdown_text: str, title: str) -> str:
    """把 Markdown 文本转换为完整 HTML 文档字符串。"""
    try:
        import markdown as md
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "缺少依赖：markdown。请先安装：pip install markdown"
        ) from e

    markdown_text = _rewrite_relative_md_links(markdown_text)

    html_body = md.markdown(
        markdown_text,
        extensions=[
            "extra",
            "tables",
            "fenced_code",
            "sane_lists",
            "toc",
        ],
        output_format="html5",
    )

    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>{safe_title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{html_body}\n"
        "</body>\n"
        "</html>\n"
    )


def convert_markdown_file(md_path: str, html_path: Optional[str] = None, *, title: Optional[str] = None) -> str:
    """把一个 Markdown 文件转换成 HTML 文件。

    Args:
        md_path: .md 文件路径
        html_path: 输出 .html 文件路径；不传则同目录同名 .html
        title: HTML 标题；不传则用文件名

    Returns:
        输出 HTML 文件的绝对路径
    """
    md_file = Path(md_path)
    if html_path is None:
        html_file = md_file.with_suffix(".html")
    else:
        html_file = Path(html_path)

    if title is None:
        title = md_file.stem

    text = md_file.read_text(encoding="utf-8", errors="ignore")
    html = markdown_to_html(text, title=title)
    html_file.parent.mkdir(parents=True, exist_ok=True)
    html_file.write_text(html, encoding="utf-8")
    return str(html_file.resolve())
