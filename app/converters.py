"""Turn every accepted upload into a PDF, so the rest of the app (page viewer, Document Analyst, OCR route,
page-follows-audio) works the same for all of them.

  PDF                        kept as is
  Photo / image (JPG, PNG, WEBP, GIF, BMP, TIFF)
                             one page per image; read like a scanned page (Document Analyst, or Bodhan OCR for
                             languages the AI cannot read). Good for a photo of a textbook page.
  Word (.docx)               text (paragraphs and tables) laid out on pages
  PowerPoint (.pptx)         one page per slide with its text
  Text (.txt, .md)           laid out on pages

For Word, PowerPoint and text files the extracted text is ALSO saved as text.json, and the reader uses it instead
of reading the text back out of the generated PDF (which can garble Indian scripts).
Returns (pdf_bytes, pages_or_None, kind) where pages is a list of pages, each a list of paragraphs.
"""
import html
import io
import re
import zipfile

import pymupdf

IMAGE_EXT = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp", ".gif": "gif", ".bmp": "bmp",
             ".tif": "tiff", ".tiff": "tiff"}
TEXT_EXT = {".txt", ".md"}
OFFICE_EXT = {".docx", ".pptx"}
ACCEPTED = [".pdf", *IMAGE_EXT, *OFFICE_EXT, *TEXT_EXT]
WORDS_PER_PAGE = 320


class UnsupportedFile(ValueError):
    pass


def _ext(name):
    m = re.search(r"\.[A-Za-z0-9]+$", name or "")
    return m.group(0).lower() if m else ""


def _sniff(name, data):
    ext = _ext(name)
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8") or data[:4] == b"GIF8" or data[:2] == b"BM" \
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP") or data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    if data.startswith(b"PK") and ext in OFFICE_EXT:
        return ext[1:]
    if ext in TEXT_EXT:
        return "text"
    raise UnsupportedFile(f"{name}: this file type is not supported. Use PDF, a photo (JPG, PNG), Word (.docx), "
                          "PowerPoint (.pptx) or a text file")


def _paginate(paras, per_page=WORDS_PER_PAGE):
    pages, cur, n = [], [], 0
    for p in paras:
        w = len(p.split())
        if cur and n + w > per_page:
            pages.append(cur)
            cur, n = [], 0
        cur.append(p)
        n += w
    if cur:
        pages.append(cur)
    return pages


def _render(pages, titles=None):
    doc = pymupdf.open()
    for i, paras in enumerate(pages):
        page = doc.new_page(width=595, height=842)
        body = "".join(f"<p>{html.escape(p)}</p>" for p in paras)
        head = f"<h2>{html.escape(titles[i])}</h2>" if titles and titles[i] else ""
        page.insert_htmlbox(pymupdf.Rect(56, 56, 539, 786), head + body,
                            css="* {font-family: sans-serif; font-size: 12pt; line-height: 1.45} h2 {font-size: 16pt}")
    out = doc.tobytes()
    doc.close()
    return out


def _image(name, data):
    try:
        img = pymupdf.open(stream=data, filetype=IMAGE_EXT.get(_ext(name), "png"))
        pdf = pymupdf.open("pdf", img.convert_to_pdf())
        img.close()
    except Exception as e:
        raise UnsupportedFile(f"{name}: the image could not be opened ({e})")
    out = pdf.tobytes()
    pdf.close()
    return out


def _docx(data):
    import docx  # python-docx
    d = docx.Document(io.BytesIO(data))
    paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for tbl in d.tables:
        for row in tbl.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                paras.append(" | ".join(dict.fromkeys(cells)))
    return _paginate(paras)


def _pptx(data):
    from pptx import Presentation  # python-pptx
    prs = Presentation(io.BytesIO(data))
    pages, titles = [], []
    for slide in prs.slides:
        title, paras = "", []
        for sh in slide.shapes:
            if sh.has_text_frame:
                txt = [p.text.strip() for p in sh.text_frame.paragraphs if "".join(r.text for r in p.runs).strip()]
                if sh == getattr(slide.shapes, "title", None) and txt:
                    title = " ".join(txt)
                else:
                    paras += [re.sub(r"\s+", " ", x) for x in txt]
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        paras.append(" | ".join(cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            paras.append(slide.notes_slide.notes_text_frame.text.strip())
        if title or paras:
            pages.append(([title] if title else []) + paras)
            titles.append(title)
    return pages, titles


def _text(data):
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            raw = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UnsupportedFile("the text file could not be decoded")
    raw = re.sub(r"^#+\s*", "", raw, flags=re.M)  # markdown headings -> plain lines
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", raw)]
    return _paginate([p for p in paras if p])


def to_pdf(name, data):
    """Convert an upload to PDF bytes. Returns (pdf_bytes, text_pages or None, kind)."""
    kind = _sniff(name, data)
    if kind == "pdf":
        return data, None, "pdf"
    if kind == "image":
        return _image(name, data), None, "image"
    try:
        if kind == "docx":
            pages, titles = _docx(data), None
        elif kind == "pptx":
            pages, titles = _pptx(data)
        else:
            pages, titles = _text(data), None
    except ImportError:
        raise UnsupportedFile(f"{name}: reading {kind} files needs an extra package. Run: pip install -r requirements.txt")
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        if isinstance(e, UnsupportedFile):
            raise
        raise UnsupportedFile(f"{name}: the file could not be read ({e})")
    if not pages:
        raise UnsupportedFile(f"{name}: no text was found in this file")
    return _render(pages, titles), pages, kind
