"""Host Framer: writes the welcome (start of the first segment) and the goodbye (end of the last segment).

The style follows the format (a class greeting for teacher-student, a show welcome for an interview, a casual
hello for friends, direct address for a narrator) and the listener's level. The text of each format's welcome and
goodbye is in config/options.yaml (formats.<id>.opening / closing); the prompt is prompts/host_framer.md.
If the model fails, a plain fallback greeting is used so the podcast is never left without one.
"""
from .. import options
from ..text import clean_line
from . import base


def _fallback(part, r, topic):
    a = r["A"]
    if part == "opening":
        if r["solo"]:
            return [{"speaker": "A", "text": f"Hello and welcome! I am {a}, and today we are learning about {topic}."}]
        return [{"speaker": "A", "text": f"Hello everyone, I am {a}."},
                {"speaker": "B", "text": f"And I am {r['B']}. Today we are learning about {topic}."}]
    text = "That is all for today. Happy learning, and see you again next time!"
    return [{"speaker": "A" if r["solo"] else "B", "text": text}]


def frame(meta, part, emit, label):
    """part: "opening" or "closing". Returns turns tagged frame=part (no source refs)."""
    s, r = meta["settings"], meta["roles"]
    fmt = options.table()["formats"][s["format"]]
    age = options.age_profile(s)

    def who(k):
        return r[k] + (f" (the {r[k + '_role'].lower()})" if r.get(k + "_role") else "")

    speakers = (f'Speaker "A" is {who("A")}; there is no other speaker.' if r["solo"]
                else f'Speaker "A" is {who("A")} and speaker "B" is {who("B")}.')
    try:
        data = base.run_json(
            "host_framer", f"Write the {part} now.",
            part=part, part_upper=part.upper(), format_title=fmt.get("title", s["format"]), speakers=speakers,
            topic=meta["title"], format_rule=(fmt.get(part) or "").strip(),
            age_rule=(age.get(part) or "").strip() + "\n" + age["prompt"].strip(),
            language_rule=options.language_rule(s.get("language", "en")),
            turn_rule="Use only speaker A." if r["solo"] else "Use 2 to 4 turns; both speakers speak.",
            max_words=45 if part == "opening" else 35,
        )
        turns = []
        for t in data.get("turns") or []:
            sp = str((t or {}).get("speaker", "")).strip().upper()[:1]
            txt = clean_line(str((t or {}).get("text", "")))
            if sp in ("A", "B") and txt:
                turns.append({"speaker": "A" if r["solo"] else sp, "text": txt})
        if not turns or len(turns) > 5:
            raise ValueError(f"{len(turns)} usable turns")
    except Exception as e:
        emit(f"Host Framer · {label}: {part} failed ({e}); using a plain {part}")
        turns = _fallback(part, r, meta["title"])
    else:
        emit(f"Host Framer · {label}: wrote the {'welcome' if part == 'opening' else 'goodbye'} ({len(turns)} lines)")
    return [{**t, "refs": [], "frame": part} for t in turns]
