"""Request Interpreter: turns a session message into scope (documents/pages/topics), needs and setting changes."""
import json
import re

from .. import options
from . import base

DIGEST_CHARS = 24000


def digest(documents, blocks_by_doc):
    """A compact page-by-page view of every ready document, for locating topics."""
    pages = []
    for d in documents:
        bl = blocks_by_doc.get(d["id"], [])
        by_page = {}
        for b in bl:
            by_page.setdefault(b["page"], []).append(b)
        pages.append((d, sorted(by_page.items())))
    total_pages = sum(len(p) for _, p in pages) or 1
    per_page = max(60, min(220, DIGEST_CHARS // total_pages))
    out = []
    for d, pg in pages:
        out.append(f"{d['id']}: {d['filename']} ({d.get('pages', '?')} pages, written in {options.language_name(d.get('language', 'en'))})")
        for n, bl in pg:
            text = " ".join(b["text"] for b in bl if b["kind"] == "text")
            visuals = [b.get("caption") or b["kind"] for b in bl if b["kind"] != "text"]
            line = f"  p{n}: {text[:per_page]}"
            if visuals:
                line += "  | visuals: " + "; ".join(visuals)
            out.append(line)
    return "\n".join(out)


def interpret(mode, text, settings, documents, blocks_by_doc, history):
    """Turn the student's request into scope (whole / page ranges / topics), needs, setting changes and a title hint."""
    t = options.table()
    data = base.run_json(
        "request_interpreter",
        f"MODE: {mode}\nREQUEST: {text.strip() or '(no extra instructions)'}",
        formats=", ".join(t["formats"]), subjects=", ".join(t["subjects"]),
        lengths=", ".join(t["lengths"]), depths=", ".join(t["depths"]),
        languages=", ".join(f"{k} ({v.get('name', v['label'])})" for k, v in t["languages"].items()),
        ages=", ".join(t["ages"]),
        history=history or "(nothing yet: this is the first podcast in this session)",
        settings=f"{options.labels(settings)}  (ids: {json.dumps(settings)})",
        digest=digest(documents, blocks_by_doc),
    )
    doc_pages = {d["id"]: d.get("pages") or 0 for d in documents}
    scope = data.get("scope") or {}
    ranges = []
    for r in scope.get("ranges") or []:
        try:
            doc, a, b = str(r["doc"]), int(r["from"]), int(r["to"])
        except (KeyError, TypeError, ValueError):
            continue
        if doc in doc_pages:
            a, b = max(1, min(a, b)), min(max(a, b), doc_pages[doc] or max(a, b))
            ranges.append({"doc": doc, "from": a, "to": b})
    changes = options.normalize(data.get("settings_changes") or {}, base={}, strict=False)
    return {
        "found": data.get("found", True) is not False,
        "understanding": str(data.get("understanding") or "").strip(),
        "whole": bool(scope.get("whole")) and mode == "full",
        "ranges": ranges,
        "topics": [str(x) for x in scope.get("topics") or [] if str(x).strip()][:6],
        "needs": [str(x).strip() for x in data.get("needs") or [] if str(x).strip()][:10],
        "settings_changes": changes,
        "title_hint": str(data.get("title_hint") or "").strip()[:120],
    }


def resolve_scope(result, mode, documents, blocks_by_doc):
    """Deterministic: the blocks the next podcast may use. Returns (blocks, how)."""
    order = [d["id"] for d in documents]
    every = [b for d in order for b in blocks_by_doc.get(d, [])]
    if result["whole"] or (mode == "full" and not result["ranges"] and not result["topics"]):
        return every, "all uploaded files" + (f" ({', '.join(order)})" if len(order) > 1 else "")
    picked = [b for b in every if any(b["doc"] == r["doc"] and r["from"] <= b["page"] <= r["to"] for r in result["ranges"])]
    if picked:
        return picked, ", ".join(f"{r['doc']} pages {r['from']}-{r['to']}" for r in result["ranges"])
    # fallback: pages that mention the topic words
    # topic words in any script: split on spaces/punctuation, keep words of 3+ characters
    keys = {w for t in result["topics"] for w in re.split(r"[\s,.;:!?()\"'।]+", t.lower()) if len(w) >= 3}
    if keys:
        hit_pages = {(b["doc"], b["page"]) for b in every if any(k in b["text"].lower() for k in keys)}
        picked = [b for b in every if (b["doc"], b["page"]) in hit_pages]
        if picked:
            return picked, "pages mentioning " + ", ".join(result["topics"])
    if mode == "full":
        return every, "all uploaded files" + (f" ({', '.join(order)})" if len(order) > 1 else "")
    return [], ""
