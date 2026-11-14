"""Small escaped Markdown reader for archived reports; never runs archived HTML."""
import base64
import html
import re
from urllib.parse import quote, urlsplit


def render_markdown(text, report_id, load_asset):
    def inline(value):
        parts, position = [], 0
        for match in re.finditer(r"(!?)\[([^\]\n]*)\]\(([^\s)]+)\)", value):
            parts.append(emphasis(html.escape(value[position:match.start()])))
            image, label, target = match.groups()
            label = html.escape(label)
            try:
                parsed = urlsplit(target)
            except ValueError:
                parts.append(label + ' <span class="missing">[原链接格式无效]</span>')
                position = match.end()
                continue
            if image:
                try:
                    if parsed.scheme or parsed.netloc or target.startswith("/"):
                        raise ValueError("External image")
                    data, media, _ = load_asset(report_id, target)
                    if media != "image/png" or len(data) > 8 * 1024 * 1024:
                        raise ValueError("Unsupported image")
                    source = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
                    parts.append(f'<figure><img src="{source}" alt="{label}" loading="lazy"><figcaption>{label}</figcaption></figure>')
                except (ValueError, KeyError, OSError):
                    parts.append(f'<span class="missing">[图片未在本报告明示附件中或不可显示：{label}]</span>')
            elif parsed.scheme in ("https", "http") and parsed.netloc and not parsed.username and not parsed.password:
                parts.append(f'<a href="{html.escape(target, quote=True)}" target="_blank" rel="noreferrer noopener">{label}</a>')
            elif target.startswith("#"):
                parts.append(f'<a href="{html.escape(target, quote=True)}">{label}</a>')
            elif not parsed.scheme and not parsed.netloc and not target.startswith("/"):
                try:
                    load_asset(report_id, target)
                    href = "/api/asset?id=" + quote(report_id, safe="") + "&path=" + quote(target, safe="")
                    parts.append(f'<a href="{href}" target="_blank" rel="noopener">{label}</a>')
                except (ValueError, KeyError, OSError):
                    parts.append(f'{label} <span class="missing">[未归档附件]</span>')
            else:
                parts.append(label)
            position = match.end()
        parts.append(emphasis(html.escape(value[position:])))
        return "".join(parts)

    def emphasis(value):
        value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)

    lines, output, paragraph = text.splitlines(), [], []
    index = 0

    def flush():
        if paragraph:
            output.append("<p>" + "<br>".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            flush()
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            output.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
        elif re.match(r"^#{1,6}\s", line):
            flush()
            level, title = line.split(None, 1)
            anchor = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title.casefold()).strip("-")
            output.append(f'<h{len(level)} id="{html.escape(anchor, quote=True)}">{inline(title)}</h{len(level)}>')
        elif "|" in line and index + 1 < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[index + 1]) and "-" in lines[index + 1]:
            flush()
            rows = [line]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            output.append('<div class="table-wrap"><table>')
            for row_index, row in enumerate(rows):
                tag = "th" if row_index == 0 else "td"
                cells = row.strip().strip("|").split("|")
                output.append("<tr>" + "".join(f"<{tag}>{inline(cell.strip())}</{tag}>" for cell in cells) + "</tr>")
            output.append("</table></div>")
            continue
        elif not line.strip():
            flush()
        elif re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line):
            flush()
            output.append('<p class="list-item">' + inline(line) + "</p>")
        elif re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            flush()
            output.append("<hr>")
        else:
            paragraph.append(line)
        index += 1
    flush()
    return ("<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>归档研究阅读</title><style>body{font:16px/1.85 'Microsoft YaHei',sans-serif;color:#203346;background:#fff;max-width:1000px;margin:auto;padding:24px}"
            "h1,h2,h3{line-height:1.4;margin-top:1.6em;color:#102a43}p{overflow-wrap:anywhere}a{color:#156f68}img{max-width:100%;height:auto}figure{margin:20px 0}figcaption,.missing{font-size:13px;color:#66757e}"
            "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #d9e1e5;padding:8px;text-align:left;vertical-align:top}th{background:#eef5f4}.table-wrap{overflow:auto}"
            "pre{background:#f3f6f7;padding:16px;overflow:auto}code{background:#f3f6f7}hr{border:0;border-top:1px solid #d9e1e5}.list-item{margin:7px 0}</style>"
            "<body><p class=missing>归档阅读版 · 原始 Markdown 保留；图片仅来自本报告明示附件。HTML 附件仅供下载。</p>" + "".join(output) + "</body></html>")
