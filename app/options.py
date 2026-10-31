"""User-facing choices (config/options.yaml): validation, labels, prompt fragments and length arithmetic."""
from . import config
from .agents.registry import _read, render

WPM = 150  # spoken words per minute

# setting -> group in options.yaml
KEYS = (("language", "languages"), ("age", "ages"), ("format", "formats"), ("subject", "subjects"),
        ("length", "lengths"), ("depth", "depths"))

# ids used by older versions -> current ids (so saved sessions keep working)
_ALIASES = {"age": {"kids": "primary", "teens": "secondary"}, "depth": {"summary": "overview", "concepts": "standard", "deep": "detailed", "basics": "standard"}}


def table():
    """The parsed config/options.yaml (re-read when the file changes)."""
    return _read(config.CONFIG_DIR / "options.yaml", parse=True)


def payload():
    """The options as sent to the interface (labels, blurbs, defaults, limits)."""
    t = table()

    def rows(key):
        return [{"id": k, "label": v["label"], "blurb": v.get("blurb", "")} for k, v in t[key].items()]

    langs = [{"id": k, "label": v["label"], "via": v.get("via"), "understood": v.get("understood", True)}
             for k, v in t["languages"].items()]
    ln = t["lengths"]["custom"]
    return {
        "languages": langs, "ages": rows("ages"), "formats": rows("formats"), "subjects": rows("subjects"),
        "lengths": rows("lengths"), "depths": rows("depths"), "defaults": t["defaults"],
        "custom_minutes": {"min": ln.get("min_minutes", 1), "max": ln.get("max_minutes", 30)},
        "parallel_limit": config.MAX_PARALLEL,
    }


def _migrate(s):
    """Settings saved by older versions -> current ids."""
    s = dict(s or {})
    if s.get("depth") == "basics":
        s["basics"] = True
    for key, aliases in _ALIASES.items():
        if s.get(key) in aliases:
            s[key] = aliases[s[key]]
    if s.get("language") == "auto":
        s["language"] = "en"
    return s


def normalize(raw, base=None, strict=True):
    """Validate choices; missing ones fall back to `base` or the defaults. strict=False drops bad values."""
    t = table()
    if base is None:
        out = dict(t["defaults"])
    elif base == {}:
        out = {}  # "changes only" (used for the interpreter's settings_changes)
    else:
        out = _migrate({**t["defaults"], **base})
    raw = _migrate(raw)
    for key, group in KEYS:
        v = raw.get(key)
        if v in (None, ""):
            continue
        if v not in t[group]:
            if strict:
                raise ValueError(f"Unknown {key}: {v}")
            continue
        out[key] = v
    if raw.get("minutes") not in (None, ""):
        ln = t["lengths"]["custom"]
        try:
            out["minutes"] = max(ln.get("min_minutes", 1), min(ln.get("max_minutes", 30), int(float(raw["minutes"]))))
        except (TypeError, ValueError):
            if strict:
                raise ValueError("minutes must be a number")
    if raw.get("depth_note") not in (None,):
        out["depth_note"] = " ".join(str(raw["depth_note"]).split())[:400]
    if raw.get("basics") not in (None, ""):
        out["basics"] = str(raw["basics"]).strip().lower() in ("1", "true", "on", "yes")
    return out


def roles(fmt):
    """Speaker names and role labels for a format: {'A', 'B', 'A_role', 'B_role', 'solo'}."""
    f = table()["formats"][fmt]
    return {"A": config.HOST_A_NAME, "B": config.HOST_B_NAME,
            "A_role": f.get("a_role"), "B_role": f.get("b_role"), "solo": bool(f.get("solo"))}


def fragment(group, key):
    """One option entry (e.g. fragment('subjects', 'science'))."""
    return table()[group][key]


def format_prompt(fmt, r):
    """The format's prompt text with the speaker names filled in."""
    return render(table()["formats"][fmt]["prompt"], a=r["A"], b=r["B"])


def depth_prompt(settings):
    """The level-of-detail prompt text (custom text included, in English)."""
    d = table()["depths"][settings["depth"]]
    text = render(d["prompt"], note=settings.get("depth_note_en") or settings.get("depth_note") or "a balanced level of detail")
    if settings.get("basics"):
        text += "\n" + table()["basics_prompt"]
    return text


def age_profile(settings):
    """The class-level profile (label, prompt, opening, closing) for the settings."""
    return table()["ages"][settings.get("age") or table()["defaults"]["age"]]


def deliveries():
    """Names of the Speech Director's deliveries."""
    return list(table()["deliveries"].keys())


def delivery_guide():
    """'name: when to use it' lines for the Speech Director."""
    return "\n".join(f"- {k}: {v.get('use', v.get('label', ''))}" for k, v in table()["deliveries"].items())


def bodhan_style(delivery, settings=None):
    """The Bodhan TTS style for a delivery, adjusted for the listener level and format (style_overrides)."""
    t = table()
    style = (t["deliveries"].get(delivery or "", {}).get("bodhan_style") or "").strip()
    if settings:
        ov = t.get("style_overrides") or {}
        for group, key in (("formats", settings.get("format")), ("ages", settings.get("age"))):
            m = (ov.get(group) or {}).get(key) or {}
            if delivery in m:
                style = (m[delivery] or "").strip()
    return style or None


def minutes(settings):
    """Chosen podcast length in minutes (custom minutes included)."""
    if settings["length"] == "custom" and settings.get("minutes"):
        return int(settings["minutes"])
    return int(table()["lengths"][settings["length"]]["minutes"])


def labels(settings):
    """A short human-readable summary of the settings."""
    t = table()
    s = _migrate(settings)
    length = f"{minutes(s)} min" if s["length"] == "custom" else f"{t['lengths'][s['length']]['label']} ({t['lengths'][s['length']]['blurb'].lower()})"
    depth = t["depths"][s["depth"]]["label"] + (f": {s.get('depth_note')}" if s["depth"] == "custom" and s.get("depth_note") else "")
    parts = [t["languages"].get(s.get("language", "en"), {}).get("label", s.get("language")),
             t["ages"].get(s.get("age", "secondary"), {}).get("label", ""),
             t["formats"][s["format"]]["label"], t["subjects"][s["subject"]]["label"], length, depth]
    if s.get("basics"):
        parts.append("background first")
    return ", ".join(p for p in parts if p)


# --- language ---

def language_name(code):
    """English name of a language code."""
    return table()["languages"].get(code, {}).get("name", "English")


def language_rule(code):
    """The instruction telling a writer which language and script to write in."""
    t = table()
    lang = t["languages"].get(code) or t["languages"]["en"]
    return render(t["language_rule"], language=lang["name"], script=lang["script"])


def write_language(code):
    """The language the agents write and check the script in (differs from `code` when it goes via translation)."""
    return (table()["languages"].get(code) or {}).get("via") or code


def llm_reads(code):
    """False for PDF languages the LLMs cannot read reliably: those PDFs go through OCR + translation first."""
    return bool((table()["languages"].get(code) or {}).get("understood", True))


def resolve_language(settings, blocks, doc_langs):
    """The podcast language. Kept for compatibility: 'auto' (older sessions) -> the PDF's language."""
    code = settings.get("language") or "en"
    if code != "auto":
        return code, False
    weight = {}
    for b in blocks:
        lang = doc_langs.get(b["doc"]) or "en"
        weight[lang] = weight.get(lang, 0) + len(b["text"])
    best = max(weight, key=weight.get) if weight else "en"
    return (best if best in table()["languages"] else "en"), True


# --- size ---

def plan_size(settings, source_words, n_blocks):
    """(number of content segments, whether to add a foundations segment)."""
    m = minutes(settings)
    n = max(2, round(m / 2.5)) if m >= 4 else 1
    n = min(n, max(1, source_words // 150), max(1, n_blocks))  # never cut short material into crumbs
    foundations = bool(settings.get("basics"))
    n = max(1, min(n, config.MAX_SEGMENTS - (1 if foundations else 0)))
    return n, foundations


FRAME_SEC = 35  # the welcome + the goodbye (Host Framer: at most 45 + 35 words)
WORDS_PER_TURN = 40  # average turn, used to estimate the pauses between speakers


def _music_seconds():
    """Time the intro/outro music adds on top of the speech."""
    extra = 0.0
    if config.music("intro"):
        extra += config.INTRO_LEAD_SEC
    out = config.music("outro")
    if out:
        try:
            from .speech import audio
            extra += max(0.0, audio.duration(out) - config.OUTRO_OVERLAP_SEC)
        except Exception:
            extra += 8.0
    return extra


def word_budget(settings, source_words, with_foundations):
    """Words the writers may use so that the FINISHED podcast (speech + pauses + welcome/goodbye + music)
    lasts about the chosen number of minutes."""
    m = minutes(settings)
    from . import speech_rates  # measured seconds per script word for this podcast language
    seconds = m * 60 - _music_seconds() - FRAME_SEC
    per_word = speech_rates.seconds_per_word(settings.get("language", "en"))
    total = max(50, int(seconds / per_word))
    found = min(int(total * 0.2), 400) if with_foundations else 0
    cap = max(80, int(source_words * float(table()["depths"][settings["depth"]]["factor"])))
    return {"content_words": min(total - found, cap), "foundation_words": found}


def segment_targets(budget, source_words_by_seg):
    """Split the content word budget over segments (half evenly, half by source length)."""
    n = len(source_words_by_seg)
    total_src = sum(source_words_by_seg) or 1
    return [
        int(max(40, min(650, budget["content_words"] * (0.5 / n + 0.5 * w / total_src))))
        for w in source_words_by_seg
    ]
