"""Speech Director: delivery style per line and speech-safe wording for formulas/symbols.

It may not change the teaching content: a "spoken" rewrite that differs too much from the
checked line is ignored. Any failure falls back to the plain line, so audio is never blocked.
"""
from .. import options
from ..text import mismatch, speech_safe
from . import base

MAX_REWRITE = 0.35  # how different (0..1) a spoken rewrite may be before it is rejected


def direct(turns, emit, label, lang="en"):
    """Set 'delivery' and a speech-safe 'speech' text on every turn, in place. Never changes what is taught."""
    names = options.deliveries()
    default = "explaining" if "explaining" in names else names[0]
    for t in turns:
        t["delivery"], t["speech"] = default, speech_safe(t["text"], lang)
    try:
        data = base.run_json(
            "speech_director",
            "\n".join(f"{i}. {t['speaker']}: {t['text']}" for i, t in enumerate(turns, 1)),
            deliveries=options.delivery_guide(),
            language_note=f"The lines are in {options.language_name(lang)}. Any \"spoken\" rewrite must stay in {options.language_name(lang)} and its script.",
        )
    except Exception as e:
        emit(f"Speech Director · {label}: skipped ({e}); using the plain lines")
        return turns
    rejected = 0
    for d in data.get("turns") or []:
        try:
            t = turns[int(d.get("turn")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        if d.get("delivery") in names:
            t["delivery"] = d["delivery"]
        spoken = " ".join(str(d.get("spoken") or "").split())
        if spoken:
            if mismatch(t["text"], spoken) <= MAX_REWRITE:
                t["speech"] = speech_safe(spoken, lang)
            else:
                rejected += 1
    if rejected:
        emit(f"Speech Director · {label}: ignored {rejected} rewrite(s) that changed too much")
    return turns
