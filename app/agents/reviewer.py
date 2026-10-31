"""Reviewer: fact-checks a segment script; its verdict decides whether the writer must revise."""
from .. import options
from . import base
from .script_writer import outline, source_text

SOLO_NOTE = ("This is a single-narrator format: judge naturalness as talk addressed directly to the listener, and do "
             "not flag the absence of a second voice.")
BAD = {"unsupported", "incorrect"}


def _language_note(code):
    name = options.language_name(code)
    return (f"The script must be in {name}. The source may be in another language: judge whether the MEANING matches "
            f"the source, not the wording. Flag any line that is not in {name} as a style issue, and any mistranslation "
            f"that changes a fact as unsupported.")


REQUEST_RULE = ("THE STUDENT ASKED FOR THIS CHANGE: \"{note}\"\n"
                "Do not report the requested change itself as a style problem. ALSO judge whether the SCRIPT carries "
                "out this request: \"request_done\" is \"yes\", \"partly\" or \"no\", and \"request_note\" says in one "
                "short sentence what was done or why it could not be (for example: the source has no information "
                "about it).")


def review(meta, seg, turns, note=None):
    """Fact-check a segment script against its source. Returns {'passed', 'issues'} and, when a change request is given, 'request' (done yes/partly/no + note)."""
    foundations = seg["kind"] == "foundations"
    script = "\n".join(f"{i}. {t['speaker']}: {t['text']}" for i, t in enumerate(turns, 1))
    kp = "KEY POINTS:\n" + "\n".join(f"- {k}" for k in seg["key_points"])
    body = f"OUTLINE:\n{outline(meta)}" if foundations else f"SOURCE:\n{source_text(seg, meta['blocks'])}"
    data = base.run_json(
        "reviewer", f"{kp}\n\n{body}\n\nSCRIPT:\n{script}",
        variant="foundations" if foundations else None,
        format_note=SOLO_NOTE if meta["roles"]["solo"] else "",
        language_note=_language_note(meta["settings"].get("language", "en")),
        request_rule=REQUEST_RULE.format(note=note.replace('"', "'")) if note else "",
    )
    issues, seen = [], set()
    for i in data.get("issues") or []:
        if isinstance(i, dict) and (i.get("type"), str(i.get("detail", "")).strip().lower()) not in seen:
            seen.add((i.get("type"), str(i.get("detail", "")).strip().lower()))
            issues.append(i)
    faithful = bool(data.get("faithful", True)) and not any(i.get("type") in BAD for i in issues)
    covers, natural = bool(data.get("covers_key_points", True)), bool(data.get("sounds_natural", True))
    out = {"passed": faithful and covers and natural, "issues": issues}
    if note:
        done = str(data.get("request_done") or "").lower()
        out["request"] = {"done": done if done in ("yes", "partly", "no") else "unknown",
                          "note": str(data.get("request_note") or "")[:300]}
    return out
