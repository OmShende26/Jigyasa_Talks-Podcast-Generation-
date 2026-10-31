"""Audience Editor: adapts an accurate script to the listener's age group (vocabulary, sentence length, tone).

The edited script then goes to the Reviewer, so an edit that changes a fact is caught there.
If the editor's reply does not have the same turns, the original script is kept unchanged.
"""
from .. import options
from ..text import clean_line
from . import base


def adapt(meta, turns, emit, label):
    """Reword a segment's turns for the listener's class level; returns the original turns if the reply is unusable."""
    s = meta["settings"]
    try:
        data = base.run_json(
            "audience_editor",
            "\n".join(f"{i}. {t['speaker']}: {t['text']}" for i, t in enumerate(turns, 1)),
            age_prompt=options.age_profile(s)["prompt"],
            language=options.language_name(s.get("language", "en")),
        )
    except Exception as e:
        emit(f"Audience Editor · {label}: skipped ({e}); keeping the script as written")
        return turns
    edits = {}
    for d in data.get("turns") or []:
        try:
            n = int(d.get("turn"))
        except (TypeError, ValueError):
            continue
        txt = clean_line(str(d.get("text") or ""))
        if txt:
            edits[n] = txt
    if set(edits) != set(range(1, len(turns) + 1)):
        emit(f"Audience Editor · {label}: reply did not match the script line for line; keeping the script as written")
        return turns
    changed = 0
    out = []
    for i, t in enumerate(turns, 1):
        new = edits[i]
        if len(new) > 1.3 * len(t["text"]) + 20:  # the editor rewords; it must not make the script longer
            new = t["text"]
        changed += new != t["text"]
        out.append({**t, "text": new})
    before, after = sum(len(t["text"].split()) for t in turns), sum(len(t["text"].split()) for t in out)
    if after > before * 1.1 + 10:
        emit(f"Audience Editor · {label}: the adapted script got longer ({before} -> {after} words); keeping the script as written")
        return turns
    emit(f"Audience Editor · {label}: adapted {changed} of {len(turns)} lines for {options.age_profile(s)['label'].lower()}")
    return out
