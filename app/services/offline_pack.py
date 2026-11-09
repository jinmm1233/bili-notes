"""离线学习包：导出一个自包含的 HTML，手机上打开就能看笔记、刷卡片、做卷子。

为什么不用 Service Worker：
  手机通过局域网 `http://192.168.x.x` 访问时不是"安全上下文"，浏览器会拒绝注册
  Service Worker——也就是说 PWA 那条路在手机上是走不通的（除非上 https）。
  所以走单文件：把数据、图片、公式全内联进一个 .html，用文件方式打开，
  断网、电脑关机、换 WiFi 都不影响。

体积控制：截图统一压到宽 900 / JPEG 78，8 篇笔记 98 张图大约 12~16MB，
微信传文件毫无压力。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..core.config import Settings
from . import cards as card_svc
from . import library
from . import quiz as quiz_svc

log = logging.getLogger(__name__)

_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_FONT = re.compile(r"url\((fonts/[^)]+\.woff2)\)")
MAX_IMAGE_EDGE = 900
JPEG_QUALITY = 78


def bundle(settings: Settings, with_markdown: bool = True) -> dict[str, Any]:
    """一次给全：笔记正文、卡片、自测卷（含答案，离线判分要用）。"""
    notes: list[dict[str, Any]] = []
    for note in library.scan(settings):
        row = {
            "id": note["id"],
            "title": note.get("title") or note["id"],
            "uploader": note.get("uploader") or "",
            "collection": note.get("collection") or library.DEFAULT_COLLECTION,
            "mtime": note.get("mtime") or 0,
        }
        if with_markdown:
            # scan() 只给元信息，正文要单独读（不然包里一篇笔记都打不开）
            try:
                row["markdown"] = library.get_note(settings, note["id"])["markdown"]
            except (FileNotFoundError, ValueError, OSError):
                row["markdown"] = ""
        notes.append(row)
    cards = card_svc.load(settings)
    quizzes = quiz_svc.load(settings)
    return {
        "generated": time.time(),
        "notes": notes,
        "cards": [
            {k: c.get(k) for k in ("id", "note_id", "note_title", "front", "back",
                                   "concept", "kind", "due", "ease", "interval",
                                   "reps", "lapses", "last")}
            for c in cards
        ],
        "quizzes": [
            {
                "id": q["id"],
                "title": q.get("title") or "",
                "kinds": q.get("kinds") or [],
                "created": q.get("created") or 0,
                "questions": [
                    {"id": x["id"], "kind": x["kind"], "stem": x["stem"],
                     "options": x.get("options") or [], "answer": x.get("answer"),
                     "explain": x.get("explain") or "", "concept": x.get("concept") or "",
                     "answer_text": quiz_svc._answer_text(x)}
                    for x in (q.get("questions") or [])
                ],
            }
            for q in quizzes
        ],
        "counts": {"notes": len(notes), "cards": len(cards), "quizzes": len(quizzes)},
    }


def _inline_image(path: Path) -> str:
    """截图压一压再内联：原始 480p 帧单张 200~400KB，不压包里会有几十兆。"""
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:                                    # 没装 Pillow 就原样内联
        data = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/jpeg;base64,{data}"

    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if w > MAX_IMAGE_EDGE:
            im = im.resize((MAX_IMAGE_EDGE, round(h * MAX_IMAGE_EDGE / w)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _collect_images(settings: Settings, notes: list[dict[str, Any]]) -> dict[str, str]:
    """{note_id/相对路径: data-uri}。读不到的图跳过，不阻断导出。"""
    root = Path(settings.output_dir)
    files: dict[str, str] = {}
    for note in notes:
        for rel in _IMG.findall(note.get("markdown") or ""):
            if rel.startswith(("http://", "https://", "data:", "/")):
                continue
            key = f"{note['id']}/{rel}"
            if key in files:
                continue
            path = root / note["id"] / rel
            if not path.is_file():
                continue
            try:
                files[key] = _inline_image(path)
            except Exception as exc:                     # 坏图不该让整包失败
                log.warning("内联图片失败 %s: %s", path, exc)
    return files


def _inline_katex(stage: Path) -> tuple[str, str]:
    """把 KaTeX 的 JS/CSS/字体全变成本地内联，公式离线也能渲染。"""
    css_path = stage / "vendor" / "katex" / "katex.min.css"
    js_path = stage / "vendor" / "katex" / "katex.min.js"
    font_dir = stage / "vendor" / "katex"
    css = css_path.read_text(encoding="utf-8")

    def repl(m: re.Match[str]) -> str:
        font = font_dir / m.group(1)
        if not font.is_file():
            return m.group(0)
        data = base64.b64encode(font.read_bytes()).decode()
        return f"url(data:font/woff2;base64,{data})"

    css = _FONT.sub(repl, css)
    js = js_path.read_text(encoding="utf-8")
    return css, js


def build_html(settings: Settings) -> str:
    """生成自包含 HTML。结构 = 原离线页 + 内联数据 + 内联 KaTeX + 内联图片。"""
    stage = Path(settings.static_dir)
    page = (stage / "offline.html").read_text(encoding="utf-8")

    data = bundle(settings)
    images = _collect_images(settings, data["notes"])
    css, js = _inline_katex(stage)

    # 1) KaTeX 换成内联
    page = page.replace(
        '<link rel="stylesheet" href="/static/vendor/katex/katex.min.css">',
        f"<style>\n{css}\n</style>",
    )
    page = page.replace(
        '<script defer src="/static/vendor/katex/katex.min.js"></script>',
        f"<script>{js}</script>",
    )
    # 2) 去掉 PWA 相关引用（file:// 下拿不到，留着只会 404）
    page = re.sub(r'\s*<link rel="manifest"[^>]*>', "", page)
    page = re.sub(r'\s*<link rel="apple-touch-icon"[^>]*>', "", page)
    # 图标也内联：file:// 下 /static/... 是找不到的（会报 ERR_FILE_NOT_FOUND）
    icon = (stage / "icon.svg").read_text(encoding="utf-8")
    page = page.replace(
        '<link rel="icon" href="/static/icon.svg" type="image/svg+xml">',
        '<link rel="icon" href="data:image/svg+xml;base64,'
        + base64.b64encode(icon.encode("utf-8")).decode() + '">',
    )
    # 单文件里没有服务器，Service Worker 注册必然失败，直接去掉
    page = page.replace("""  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () { /* file:// 或 http 下不可用 */ });
  }""", "  /* 单文件：不需要 Service Worker */")

    # 3) 数据与图片内联在页面脚本之前
    payload = json.dumps({"bundle": data, "files": images}, ensure_ascii=False)
    page = page.replace(
        "<script>\n(function () {",
        f"<script>window.__BN_OFFLINE__={payload};</script>\n<script>\n(function () {{",
        1,
    )
    if "__BN_OFFLINE__" not in page:
        # 兜底：直接塞在 </body> 前（脚本读不到就退化成在线的 fetch）
        page = page.replace("</body>", f"<script>window.__BN_OFFLINE__={payload};</script>\n</body>")

    title = time.strftime("%Y%m%d")
    page = page.replace(
        "<title>BiliNotes · 离线学习包</title>",
        f"<title>BiliNotes 学习包 {title}</title>",
    )
    return page
