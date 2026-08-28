from __future__ import annotations
import re
import zipfile
import xml.etree.ElementTree as ET
import warnings
from pathlib import Path
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
warnings.filterwarnings("ignore", category=UserWarning)


def _extract_chapter_title(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in ("h1", "h2", "h3"):
        tag = soup.find(tag_name)
        if tag:
            text = tag.get_text(" ", strip=True)
            if text:
                return text
    return fallback


def _html_to_plain(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body") or soup
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
             "li", "blockquote", "pre", "div", "td", "th"}
    parts: list[str] = []
    for tag in body.find_all(BLOCK):
        if any(p.name in BLOCK for p in tag.parents if p != body):
            continue
        t = tag.get_text(" ", strip=True)
        if t:
            parts.append(t)
    if parts:
        return "\n\n".join(parts)
    return body.get_text("\n\n", strip=True)


def _zip_extract_epub(epub_path: Path) -> tuple[str, list[str]]:
    with zipfile.ZipFile(epub_path, "r") as z:
        rootfile_path = "OEBPS/content.opf"
        try:
            container_xml = z.read("META-INF/container.xml")
            c_root = ET.fromstring(container_xml)
            for elem in c_root.iter():
                if elem.tag.endswith("rootfile"):
                    fp = elem.attrib.get("full-path")
                    if fp:
                        rootfile_path = fp
                        break
        except Exception:
            pass

        opf_dir = str(Path(rootfile_path).parent).replace("\\", "/")
        if opf_dir == ".":
            opf_dir = ""

        opf_xml = z.read(rootfile_path)
        opf_root = ET.fromstring(opf_xml)

        title = epub_path.stem
        for elem in opf_root.iter():
            if elem.tag.endswith("title") and elem.text:
                title = elem.text.strip()
                break

        manifest = {}
        for elem in opf_root.iter():
            if elem.tag.endswith("item"):
                i_id = elem.attrib.get("id")
                href = elem.attrib.get("href")
                if i_id and href:
                    full_href = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
                    manifest[i_id] = full_href

        spine_items = []
        for elem in opf_root.iter():
            if elem.tag.endswith("itemref"):
                idref = elem.attrib.get("idref")
                if idref in manifest:
                    spine_items.append(manifest[idref])

        if not spine_items:
            spine_items = [v for k, v in manifest.items() if v.lower().endswith((".xhtml", ".html", ".htm"))]

        raw_htmls = []
        for p in spine_items:
            try:
                raw = z.read(p).decode("utf-8", errors="replace")
                if raw:
                    raw_htmls.append(raw)
            except Exception:
                pass

        return title, raw_htmls


def extract_chapters(epub_path: Path) -> tuple[str, list[dict]]:
    book_title = epub_path.stem
    raw_htmls: list[str] = []

    try:
        import ebooklib
        from ebooklib import epub

        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        book_title = book.title or epub_path.stem

        def _is_real_chapter(item) -> bool:
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                return False
            is_chapter_fn = getattr(item, "is_chapter", None)
            if callable(is_chapter_fn):
                return bool(is_chapter_fn())
            return True

        for item_id, _ in book.spine:
            item = book.get_item_with_id(item_id)
            if item and _is_real_chapter(item):
                raw = item.get_content()
                if raw:
                    raw_htmls.append(raw.decode("utf-8", errors="replace"))

        if not raw_htmls:
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                if _is_real_chapter(item):
                    raw = item.get_content()
                    if raw:
                        raw_htmls.append(raw.decode("utf-8", errors="replace"))
    except Exception:
        try:
            t, htmls = _zip_extract_epub(epub_path)
            book_title = t or book_title
            raw_htmls = htmls
        except Exception as e:
            raise RuntimeError(f"無法解析 EPUB 檔案: {e}")

    chapters: list[dict] = []
    n = 0
    for html in raw_htmls:
        text = _html_to_plain(html)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 30:
            continue
        n += 1
        title = _extract_chapter_title(html, fallback=f"第 {n} 章")
        chapters.append({"index": n, "title": title, "text": text, "char_count": len(text)})

    return book_title, chapters
