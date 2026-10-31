"""Learning Designer: plans the segments of one podcast over the selected source blocks."""
from .. import options
from ..text import words
from . import base

FOUNDATION_RULE = (
    '- ALSO give "foundations": the 3 to 5 prerequisite ideas that a student who is new to this topic must '
    "understand before the material makes sense (school-level background it assumes but does not teach). Give a "
    "short title and key_points, each one short sentence naming an idea to explain. Only ideas the material really relies on."
)


def _even_split(blocks, k):
    total = sum(words(b["text"]) for b in blocks) or 1
    per, segs, start, acc = total / k, [], 0, 0
    for i, b in enumerate(blocks):
        acc += words(b["text"])
        if acc >= per and len(segs) < k - 1:
            segs.append({"title": f"Part {len(segs) + 1}", "start": start, "end": i, "key_points": []})
            start, acc = i + 1, 0
    if start < len(blocks):
        segs.append({"title": f"Part {len(segs) + 1}", "start": start, "end": len(blocks) - 1, "key_points": []})
    return segs


def _covers_each(segs, blocks, docs):
    """Every file is the main source of at least one segment."""
    main = set()
    for s in segs:
        part = [b["doc"] for b in blocks[s["start"]: s["end"] + 1]]
        if part:
            main.add(max(set(part), key=part.count))
    return set(docs) <= main


def _split_by_document(blocks, docs, n):
    """n segments shared out over the files by their length (at least one each), never mixing two files."""
    by = {d: [i for i, b in enumerate(blocks) if b["doc"] == d] for d in docs}
    size = {d: sum(words(blocks[i]["text"]) for i in by[d]) or 1 for d in docs}
    share = {d: 1 for d in docs}
    for _ in range(max(0, n - len(docs))):
        d = max(docs, key=lambda x: size[x] / share[x])
        share[d] += 1
    out = []
    for d in docs:
        idx = by[d]
        part = _even_split([blocks[i] for i in idx], share[d])
        for s in part:
            out.append({"title": s["title"], "start": idx[s["start"]], "end": idx[s["end"]], "key_points": []})
    for i, s in enumerate(out):
        s["title"] = f"Part {i + 1}"
    return out


def _validate(raw, index, max_segments):
    """Planner output -> list of {title, start, end, key_points} with 0-based block indexes, in order, no overlap."""
    items = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        a, b = index.get(str(s.get("first_block"))), index.get(str(s.get("last_block")))
        if a is None or b is None:
            continue
        items.append((min(a, b), max(a, b), s))
    out = []
    for a, b, s in sorted(items, key=lambda x: x[0]):
        if out:
            if a <= out[-1]["end"]:
                a = out[-1]["end"] + 1
            elif a > out[-1]["end"] + 1:
                out[-1]["end"] = a - 1  # close gaps
        if a > b:
            continue
        kp = [str(k) for k in s.get("key_points") or [] if str(k).strip()]
        out.append({"title": str(s.get("title") or f"Part {len(out) + 1}").strip(), "start": a, "end": b, "key_points": kp})
    while len(out) > max_segments:
        last = out.pop()
        out[-1]["end"] = last["end"]
        out[-1]["key_points"] += last["key_points"]
    return out


def plan(blocks, settings, needs, emit, doc_names=None):
    """Returns (episode_title, segments). Each segment has kind, title, key_points, blocks, target_words."""
    src_words = sum(words(b["text"]) for b in blocks)
    n, with_found = options.plan_size(settings, src_words, len(blocks))
    depth = options.fragment("depths", settings["depth"])
    listener = (
        f"subject {options.fragment('subjects', settings['subject'])['label']}, depth '{depth['label']}', "
        f"length about {options.minutes(settings)} minutes, audience {options.age_profile(settings)['label'].lower()}"
        + (f", detail wanted: {settings.get('depth_note_en') or settings.get('depth_note')}" if settings["depth"] == "custom" and settings.get("depth_note") else "")
    )
    # a short podcast cannot cover many points: scale the key points to the words each segment has
    per_seg = options.word_budget(settings, src_words, with_found)["content_words"] / max(1, n)
    key_points = "1 to 2" if per_seg < 160 else "2 to 3" if per_seg < 320 else depth["key_points"]
    index = {b["id"]: i for i, b in enumerate(blocks)}
    docs = list(dict.fromkeys(b["doc"] for b in blocks))
    if len(docs) > 1:
        names = doc_names or {}
        n = max(n, min(len(docs), options.config.MAX_SEGMENTS)) if options.minutes(settings) >= 2 * len(docs) else n
        rule = (f"- The material comes from {len(docs)} different files: "
                + "; ".join(f"{d} = '{names.get(d, d)}'" for d in docs) + ". EVERY file must be covered. "
                + ("Give each file its own segment(s); do not mix two files in one segment."
                   if n >= len(docs) else "Include key points from every file, not just the first one."))
    else:
        rule = ""
    emit("Learning Designer: planning the segments")
    title, segs, found = None, [], None
    try:
        data = base.run_json(
            "learning_designer",
            "\n\n".join(f"[{b['id']}] {b['text']}" for b in blocks),
            listener=listener, n=n, key_points=key_points,
            needs="\n".join(f"- {x}" for x in needs) or "(none)",
            foundation_rule=FOUNDATION_RULE if with_found else "", documents_rule=rule,
            foundation_shape='{"title": "...", "key_points": ["..."]}' if with_found else "null",
        )
        title = str(data.get("episode_title") or "").strip() or None
        segs = _validate(data.get("segments"), index, n)
        f = data.get("foundations")
        if with_found and isinstance(f, dict):
            kp = [str(k) for k in f.get("key_points") or [] if str(k).strip()]
            if kp:
                found = {"title": str(f.get("title") or "The basics").strip(), "key_points": kp[:6]}
    except Exception as e:  # a planning failure must not kill the run
        emit(f"Learning Designer: plan failed ({e}); falling back to an even split")
    if not segs:
        segs = _even_split(blocks, n)
    if len(docs) > 1 and n >= len(docs) and not _covers_each(segs, blocks, docs):
        emit("Learning Designer: the plan left out a file; giving every file its own segment(s)")
        segs = _split_by_document(blocks, docs, n)
    if with_found and not found:
        emit("Learning Designer: no prerequisite ideas identified, so there is no basics segment")

    out = []
    for s in segs:
        out.append({"kind": "content", "title": s["title"], "key_points": s["key_points"],
                    "block_ids": [b["id"] for b in blocks[s["start"]: s["end"] + 1]]})
    budget = options.word_budget(settings, src_words, bool(found))
    by_id = {b["id"]: b for b in blocks}
    seg_words = [sum(words(by_id[i]["text"]) for i in s["block_ids"]) for s in out]
    for s, t in zip(out, options.segment_targets(budget, seg_words)):
        s["target_words"] = t
    if found:
        out.insert(0, {"kind": "foundations", "title": found["title"], "key_points": found["key_points"],
                       "block_ids": [], "target_words": max(120, budget["foundation_words"])})
    emit(f"Learning Designer: {len(out)} segments planned" + (" (the first explains the basics)" if found else ""))
    return title or "Podcast", out
