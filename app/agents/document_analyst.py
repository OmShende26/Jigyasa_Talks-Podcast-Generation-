"""Document Analyst: reads a page image (scans, figures, tables, equations) with a multimodal model."""
from . import base

KINDS = {"figure", "diagram", "graph", "map", "table", "equation"}


def analyze_page(png_bytes, page_no, text_layer):
    """Read one page image: returns {'transcription': str, 'visuals': [{'kind', 'caption', 'description'}]}."""
    data = base.run_json(
        "document_analyst",
        f"Page {page_no}.\nText extracted digitally from this page:\n{(text_layer or '').strip()[:4000] or '(none)'}",
        images=[png_bytes],
    )
    visuals = []
    for v in data.get("visuals") or []:
        if not isinstance(v, dict):
            continue
        desc = " ".join(str(v.get("description") or "").split())
        if len(desc) < 20:
            continue
        kind = str(v.get("kind") or "figure").lower()
        visuals.append({
            "kind": kind if kind in KINDS else "figure",
            "caption": " ".join(str(v.get("caption") or "").split())[:120],
            "description": desc[:900],
        })
    return {"transcription": str(data.get("transcription") or "").strip(), "visuals": visuals[:6]}
