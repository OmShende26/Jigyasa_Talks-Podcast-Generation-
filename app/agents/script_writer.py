"""Script Writer: drafts and revises the spoken script for one segment, with a source reference per turn."""
from .. import options
from ..text import clean_line
from . import base

FAITH_SOURCE = """- Every fact, number, definition, name, date and cause-and-effect claim must come from the SOURCE. Do not add outside facts and never contradict the source.
- No invented stories, studies, statistics, quotes or personal experiences.
- You may add short everyday analogies and rhetorical questions. An analogy must add no new factual claim and should sound like one ("think of it like...").
- Cover every key point.
- Source blocks that start with [Figure ...], [Table ...], [Equation ...] etc. describe what is on the page. The listener cannot see the page, so explain what it shows in your own words as part of the explanation, using only what the block says. Never say "as you can see" or "look at the figure"."""

FAITH_FOUNDATIONS = """- This segment has no source text. It explains the prerequisite ideas listed under KEY POINTS, using well-established school-level knowledge, so that a student who is new to the topic can follow the rest.
- Be accurate. Keep to what is needed for the outline you are given; do not go into the material's own detail yet.
- No invented stories, studies, statistics, quotes or personal experiences.
- Cover every key point, then lead into the first topic of the outline."""


def _position_rule(idx, total, prev_title, next_title, foundations, age, topic):
    # the welcome and the goodbye are written separately by the Host Framer
    opening = ("START OF THE PODCAST: the welcome and the speakers' introductions are added separately just before "
               "your script, so do NOT greet, do NOT introduce names and do NOT announce the topic: begin directly "
               "with the first idea in a natural way.")
    closing = ("END OF THE PODCAST: a goodbye is added separately right after your script, so do NOT say goodbye, "
               "do NOT say happy learning or see you next time: end on a natural final thought about the content.")
    if total == 1:
        return f"- This is the only segment. {opening} {closing}"
    if idx == 0:
        lead = " It is the basics part, so say up front that you will start from the ground up." if foundations else ""
        return (f"- This is the OPENING segment. {opening}{lead} "
                f"The next segment is '{next_title}', so do not wrap up the episode.")
    if idx == total - 1:
        return (f"- This is the FINAL segment. Continue naturally from the previous segment ('{prev_title}') without "
                f"re-introducing the speakers, teach the content, then: {closing}")
    return (f"- This is a MIDDLE segment. Continue naturally from the previous segment ('{prev_title}') without "
            f"re-introducing the show or wrapping up; the next segment is '{next_title}'. Do not say goodbye.")


def outline(meta):
    """The episode outline (segment titles and key points) as text, for the foundations segment."""
    return "\n".join(f"- {o['title']}: " + "; ".join(o["key_points"]) for o in meta.get("outline", []))


def source_text(seg, blocks_by_id):
    """The segment's source blocks as '[block id] text' lines."""
    return "\n\n".join(f"[{i}] {blocks_by_id[i]['text']}" for i in seg["block_ids"] if i in blocks_by_id)


def write(meta, seg, *, draft=None, issues=None, note=None):
    """Write (or revise) one segment's dialogue. Returns [{'speaker': 'A'|'B', 'text', 'refs': [block ids]}]."""
    s, r = meta["settings"], meta["roles"]
    foundations = seg["kind"] == "foundations"
    fmt = options.fragment("formats", s["format"])
    i, total = seg["idx"], meta["total"]
    def who(k):
        return r[k] + (f" (the {r[k + '_role'].lower()})" if r.get(k + "_role") else "")
    speakers = (f'In the JSON, speaker "A" is {who("A")}; use no other speaker.' if r["solo"]
                else f'In the JSON, speaker "A" is {who("A")} and speaker "B" is {who("B")}.')
    parts = [
        f"PODCAST: {meta['title']}",
        f"SEGMENT {i + 1} of {total}: {seg['title']}",
        "KEY POINTS TO COVER:\n" + "\n".join(f"- {k}" for k in seg["key_points"]),
    ]
    if meta.get("needs"):
        parts.append("THE STUDENT'S REQUEST FOR THIS PODCAST:\n" + "\n".join(f"- {x}" for x in meta["needs"]))
    if foundations:
        parts.insert(2, "KIND: FOUNDATIONS")
        parts.append("OUTLINE (what the podcast covers after this segment):\n" + outline(meta))
    else:
        parts.append("SOURCE (the only allowed source of facts):\n" + source_text(seg, meta["blocks"]))
        seg_docs = list(dict.fromkeys(i.split("p")[0] for i in seg["block_ids"]))
        if len(seg_docs) > 1:
            parts.append(f"NOTE: this SOURCE comes from {len(seg_docs)} different files ({', '.join(seg_docs)}). "
                         "Teach from every one of them, not only the first.")
    if draft:
        parts.append("PREVIOUS DRAFT:\n" + "\n".join(f"{t['speaker']}: {t['text']}" for t in draft))
    if issues:
        parts.append("A FACT-CHECKER FOUND THESE PROBLEMS. Fix every one of them in the new version:\n"
                     + "\n".join(f"- [{x.get('type')}] {x.get('detail')} (fix: {x.get('fix')})" for x in issues))
    if note:
        parts.append("REVIEWER INSTRUCTION - THE STUDENT ASKED FOR THIS CHANGE. Carry it out fully and visibly in the new "
                     "version (it is the main purpose of this rewrite). Keep everything else as good as before. If it "
                     "needs facts that are not in the SOURCE, do as much as the source allows and never invent facts:\n"
                     + note)
    parts.append("Write the complete revised script now." if draft else "Write the complete script for this segment now.")

    values = dict(
        format_title=fmt["title"], format_prompt=options.format_prompt(s["format"], r), speakers=speakers,
        subject_prompt=options.fragment("subjects", s["subject"])["prompt"],
        depth_prompt=options.depth_prompt(s),
        age_prompt=options.age_profile(s)["prompt"],
        faithfulness=FAITH_FOUNDATIONS if foundations else FAITH_SOURCE,
        position_rule=_position_rule(i, total, meta["titles"][i - 1] if i else None,
                                     meta["titles"][i + 1] if i + 1 < total else None, foundations,
                                     options.age_profile(s), meta["title"]),
        target_words=seg.get("target_words") or 250,
        language_rule=options.language_rule(s.get("language", "en")),
    )
    allowed = set(seg["block_ids"])
    short = (seg.get("target_words") or 250) < 150  # a 1-2 minute podcast can be just a few turns
    min_turns, last = ((1 if short else 3) if r["solo"] else (2 if short else 4)), None
    for _ in range(2):
        data = base.run_json("script_writer", "\n\n".join(parts), **values)
        turns = []
        for t in data.get("turns") or []:
            if not isinstance(t, dict):
                continue
            sp = str(t.get("speaker", "")).strip().upper()[:1]
            txt = clean_line(str(t.get("text", "")))
            if sp in ("A", "B") and txt:
                refs = [x for x in (t.get("refs") or []) if isinstance(x, str) and x in allowed]
                turns.append({"speaker": "A" if r["solo"] else sp, "text": txt, "refs": refs})
        if len(turns) >= min_turns:
            return turns
        last = f"only {len(turns)} usable turns"
    raise ValueError(f"Script Writer returned an unusable script ({last})")
