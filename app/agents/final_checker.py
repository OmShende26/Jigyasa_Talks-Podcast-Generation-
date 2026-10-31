"""Final Checker: the last look at a segment before text-to-speech.

It compares, line by line, the APPROVED line (what the Reviewer checked against the source) with what will
actually be SPOKEN, and reports changed numbers, flipped negations, swapped terms and missing or added meaning.

Why a separate light agent and not the Reviewer again: the Reviewer needs the whole source and is the expensive
call; this check only needs the pairs of lines, so a small fast model (config/agents.yaml: final_checker)
is enough and the cost is one short call per segment. Only lines that changed are sent:
  - translated languages: every line (English original -> translation)
  - English / Hindi: only lines the Speech Director rewrote for speech (formulas, symbols written as words)
"""
from .. import options
from . import base


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _same_value(a, b):
    """Deterministic double-check of a number issue: equal values (within 0.5%) are not a mistake."""
    a, b = _num(a), _num(b)
    return a is not None and b is not None and abs(a - b) <= 0.005 * max(abs(a), abs(b), 1e-9)


def pairs(turns):
    """[(line number, approved, spoken)] for the lines whose spoken form differs from the approved one."""
    out = []
    for i, t in enumerate(turns, 1):
        approved = t.get("original") or t["text"]
        spoken = t.get("speech") or t["text"]
        if " ".join(approved.split()) != " ".join(spoken.split()):
            out.append((i, approved, spoken))
    return out


def check(turns, source_lang, target_lang, only=None):
    """{line number: issue} for the lines that do not match. only = restrict to these line numbers."""
    items = [p for p in pairs(turns) if only is None or p[0] in only]
    if not items:
        return {}
    text = "\n\n".join(f"LINE {n}\nAPPROVED: {a}\nSPOKEN: {s}" for n, a, s in items)
    data = base.run_json("final_checker", text, source_language=options.language_name(source_lang),
                         target_language=options.language_name(target_lang))
    wanted = {n for n, _, _ in items}
    found = {}
    for x in data.get("issues") or []:
        try:
            n = int(x.get("line"))
        except (TypeError, ValueError, AttributeError):
            continue
        if n not in wanted:
            continue
        typ = str(x.get("type") or "other")
        if typ == "number" and _same_value(x.get("approved_value"), x.get("spoken_value")):
            continue  # e.g. "10 lakh" vs "1 million": the checker itself says the values are equal
        found[n] = {"line": n, "type": typ, "detail": str(x.get("detail") or "")[:200]}
    return found
