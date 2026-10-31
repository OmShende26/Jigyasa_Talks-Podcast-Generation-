"""PDF ingest: page images, text blocks, scanned pages and visuals.

Two routes, chosen by the PDF's language (detected, or set by the user on the document chip):
  direct          (languages the LLMs read, see `understood` in config/options.yaml)
                  digital text as extracted + the multimodal Document Analyst for scans and visuals.
  ocr_translate   (languages the LLMs cannot read)
                  every page -> Bodhan OCR -> Bodhan translation to English. Blocks hold the English text
                  (what the agents read) and keep the OCR text as "original". Visuals are still described
                  by the Document Analyst from the page image.

Each block: {"id": "d1p3b2", "doc": "d1", "page": 3, "kind": "text"|"figure"|"table"|..., "text": "...", "caption": "..."}
Digital text is kept exactly as extracted. The multimodal Document Analyst is only asked about pages that
have no usable text layer or that show signs of a figure, table or equation.
"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pymupdf

from . import config, store
from . import options, translation
from .agents import document_analyst
from .speech import bodhan
from .text import detect_language

_NOISE = [
    re.compile(r"^\s*\d{1,3}\s*$"),  # bare page numbers
    re.compile(r"(?i)^\s*(reprint|rationali[sz]ed)\b.*\d{4}.*$"),  # NCERT footer stamps
]
_CAPTION = re.compile(r"\b(?:fig(?:ure)?|table)\.?\s*(\d+(?:[.\-]\d+)*)", re.I)
_MATHY = re.compile(r"[=±×÷√∑∫≤≥≠→∆πθλμ²³]")


def _clean(s):
    s = s.replace("­", "")
    s = re.sub(r"-\n(?=[a-z])", "", s)
    s = re.sub(r"\s*\n\s*", " ", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def _texts(page):
    blocks = [b for b in page.get_text("blocks", sort=True) if b[6] == 0]
    out = [_clean(b[4]) for b in blocks]
    return [t for t in out if t and not any(p.match(t) for p in _NOISE)]


def _garbled(texts):
    """True when an Indian-script text layer is broken (common with older fonts in Indian-language PDFs):
    vowel signs or viramas detached at the start of words, dotted circles, private-use characters.
    Such pages are read from the page image instead, so the agents never see damaged text."""
    import unicodedata
    joined = " ".join(texts)
    if any(0xE000 <= ord(c) <= 0xF8FF for c in joined):
        return True
    words_ = [w for w in joined.split() if any(0x0900 <= ord(c) <= 0x0DFF for c in w)]
    if len(words_) < 12:
        return False
    bad = sum(1 for w in words_ if unicodedata.category(w[0]) in ("Mn", "Mc") or "\u25cc" in w
              or any(unicodedata.category(a) == "Mn" and unicodedata.category(b) == "Mn" and a == b for a, b in zip(w, w[1:])))
    return bad / len(words_) > 0.04


def _needs_vision(page, texts):
    """(scanned, has_visual_signal)."""
    if texts and _garbled(texts):
        return True, True  # read the page image: the text layer is damaged
    has_pictures = bool(page.get_images())
    scanned = sum(len(t) for t in texts) < 40 and has_pictures
    if scanned:
        return True, True
    joined = " ".join(texts)
    if _CAPTION.search(joined) or len(_MATHY.findall(joined)) >= 4:
        return False, True
    area = page.rect.width * page.rect.height
    try:
        img_area = sum(pymupdf.Rect(i["bbox"]).width * pymupdf.Rect(i["bbox"]).height for i in page.get_image_info())
        if img_area > 0.06 * area:
            return False, True
    except Exception:
        pass
    try:
        if len(page.get_drawings()) > 30:
            return False, True
    except Exception:
        pass
    return False, False


def _split_paragraphs(raw):
    parts = re.split(r"\n\s*\n", raw.strip())
    if len(parts) == 1 and "\n" in raw:
        parts = raw.strip().split("\n")
    out = [_clean(p) for p in parts]
    return [p for p in out if p and not any(n.match(p) for n in _NOISE)]


def _visual_text(v, page):
    label = f", {v['caption']}" if v["caption"] else ""
    return f"[{v['kind'].capitalize()} on page {page}{label}] {v['description']}"


def _place_visuals(texts, visuals, page):
    """Text blocks plus visual blocks; a visual goes right after its printed caption when the caption is found."""
    items = [("text", t, "") for t in texts]
    for v in visuals:
        entry = (v["kind"], _visual_text(v, page), v["caption"])
        num = _CAPTION.search(v["caption"] or "")
        pos = len(items)
        if num:
            for j, (kind, t, _) in enumerate(items):
                m = _CAPTION.match(t.strip()) if kind == "text" else None
                if m and m.group(1) == num.group(1):
                    pos = j + 1
                    break
        items.insert(pos, entry)
    return items


def analyze(sid, did):
    """Read an uploaded document in a background thread (page images, text blocks, visuals)."""
    threading.Thread(target=_analyze, args=(sid, did), daemon=True, name=f"doc-{sid}-{did}").start()


def _set_doc(sid, did, **kw):
    store.update(sid, lambda st: store.find(st["documents"], did).update(kw))


def _ocr_translate_page(png_hi, pg, lang, name, emit):
    """One page through Bodhan OCR, then each paragraph translated to English. Returns [(english, original)]."""
    raw = bodhan.ocr(png_hi)
    paras = _split_paragraphs(raw)
    out = []
    for para in paras:
        try:
            out.append((translation.translate_text(para, "en"), para))
        except Exception as e:
            emit(f"{name} page {pg}: one paragraph could not be translated to English ({e}); it is left out")
    return out


def _analyze(sid, did):
    emit = lambda m: store.log(sid, m)  # noqa: E731
    try:
        ddir = store.session_dir(sid) / "docs" / did
        d0 = store.find(store.get(sid)["documents"], did)
        name = d0["filename"]
        emit(f"Reading {name}")
        doc = pymupdf.open(ddir / "source.pdf")
        total, n = len(doc), min(len(doc), config.MAX_PDF_PAGES)
        (ddir / "pages").mkdir(exist_ok=True)
        page_texts, pngs, scanned, signal, hi_res = {}, {}, [], [], {}
        given = None  # Word / PowerPoint / text uploads: their original text, page by page
        if (ddir / "text.json").exists():
            import json
            given = json.loads((ddir / "text.json").read_text(encoding="utf-8"))
        for i in range(n):
            page, pg = doc[i], i + 1
            png = page.get_pixmap(dpi=config.PAGE_DPI).tobytes("png")
            (ddir / "pages" / f"p{pg}.png").write_bytes(png)
            if given is not None:
                texts = [_clean(x) for x in (given[i] if i < len(given) else []) if _clean(x)]
                page_texts[pg] = texts
                is_scan, has_signal = (False, False)
            else:
                texts = _texts(page)
                page_texts[pg] = texts
                is_scan, has_signal = _needs_vision(page, texts)
            if is_scan:
                scanned.append(pg)
            elif has_signal:
                signal.append(pg)
            if is_scan or has_signal:
                pngs[pg] = png
            hi_res[pg] = page  # rendered at OCR resolution only if needed
        # language: set by the user, or detected from the text layer
        if d0.get("language_set"):
            language = d0["language"]
        else:
            language = detect_language(" ".join(" ".join(t) for t in page_texts.values())[:20000])
        route = "direct" if options.llm_reads(language) else "ocr_translate"
        ocr_pngs = {pg: hi_res[pg].get_pixmap(dpi=200).tobytes("png") for pg in hi_res} if route == "ocr_translate" else {}
        doc.close()
        _set_doc(sid, did, pages=n, total_pages=total, language=language, route=route)
        if total > n:
            emit(f"{name}: only the first {n} of {total} pages are used")

        translated = {}
        if route == "ocr_translate" and not config.BODHAN_TRANSLATE_API_KEY:
            raise ValueError(f"{options.language_name(language)} PDFs are read with Bodhan OCR and then translated: "
                             "add BODHAN_TRANSLATE_API_KEY to .env and restart")
        if route == "ocr_translate":
            emit(f"{name} is in {options.language_name(language)}: reading it with Bodhan OCR and translating it to English for the agents")

            def ocr_run(pg):
                try:
                    return pg, _ocr_translate_page(ocr_pngs[pg], pg, language, name, emit), None
                except Exception as e:
                    return pg, None, e

            with ThreadPoolExecutor(max_workers=2) as pool:
                for pg, res, err in pool.map(ocr_run, sorted(ocr_pngs)):
                    if err:
                        emit(f"{name} page {pg}: OCR or translation failed ({err})")
                    else:
                        translated[pg] = res
            scanned = []  # OCR covers the text of every page; the analyst only describes visuals

        todo = (scanned + signal)[: config.MAX_VISION_PAGES]  # scanned pages first: without them there is no text
        skipped = len(scanned) + len(signal) - len(todo)
        results = {}
        if todo:
            emit(f"Document Analyst: looking at {len(todo)} page(s) of {name} with scans, figures, tables or equations")

            def run(pg):
                context = "\n".join(e for e, _ in translated[pg]) if pg in translated else "\n".join(page_texts[pg])
                try:
                    return pg, document_analyst.analyze_page(pngs[pg], pg, context), None
                except Exception as e:
                    return pg, None, e

            with ThreadPoolExecutor(max_workers=config.ANALYST_WORKERS) as pool:
                for pg, res, err in pool.map(run, todo):
                    if err:
                        emit(f"{name} page {pg}: the Document Analyst failed ({err}); using the text only")
                    else:
                        results[pg] = res

        blocks, n_visuals, unread = [], 0, []
        for pg in range(1, n + 1):
            originals = {}
            if route == "ocr_translate":
                pairs = translated.get(pg) or []
                texts = [e for e, _ in pairs]
                originals = {e: o for e, o in pairs}
                if not texts:
                    unread.append(pg)
            else:
                texts = page_texts[pg]
                res0 = results.get(pg)
                if pg in scanned:
                    texts = _split_paragraphs(res0["transcription"]) if res0 and res0["transcription"] else []
                    if not texts:
                        unread.append(pg)
            res = results.get(pg)
            visuals = res["visuals"] if res else []
            n_visuals += len(visuals)
            for k, (kind, text, caption) in enumerate(_place_visuals(texts, visuals, pg), 1):
                b = {"id": f"{did}p{pg}b{k}", "doc": did, "page": pg, "kind": kind, "text": text}
                if caption:
                    b["caption"] = caption
                if text in originals:
                    b["original"] = originals[text]
                blocks.append(b)
        if not blocks:
            raise ValueError("No readable text found in this file")
        if not d0.get("language_set") and route == "direct" and scanned and not any(page_texts.values()):
            # a photo or scan: the language is only known after the Document Analyst has read the page
            seen = detect_language(" ".join(b["text"] for b in blocks if b["kind"] == "text")[:20000])
            if seen != language:
                language = seen
                _set_doc(sid, did, language=language)
        store.save_blocks(sid, did, blocks)
        info = {"scanned_pages": scanned, "vision_pages": sorted(results), "visuals": n_visuals,
                "unread_pages": unread, "vision_skipped": skipped, "blocks": len(blocks),
                "ocr_pages": sorted(translated) if route == "ocr_translate" else []}
        _set_doc(sid, did, status="ready", error=None, info=info)
        msg = f"{name} is ready: {n} page(s), {len(blocks)} blocks, {n_visuals} figure/table/equation description(s)"
        if route == "ocr_translate":
            msg += f"; read with OCR and translated from {options.language_name(language)}"
        if unread:
            msg += f"; could not read page(s) {', '.join(map(str, unread))}"
        if skipped:
            msg += f"; {skipped} page(s) with visuals were not analysed (limit MAX_VISION_PAGES)"
        emit(msg)

        def retitle(st):  # named after the FIRST PDF, whichever finishes reading first
            if st["title"] == "New session":
                st["title"] = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", st["documents"][0]["filename"])[:80]
        store.update(sid, retitle)
    except Exception as e:
        emit(f"Could not read the PDF: {e}")
        _set_doc(sid, did, status="error", error=str(e)[:300])
