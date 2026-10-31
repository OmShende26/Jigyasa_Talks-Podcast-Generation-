"""Small text utilities shared by several modules."""
import difflib
import re

from . import config


def words(s):
    """Word count that works for every script (Indic vowel signs break \\w-based counting)."""
    return len((s or "").split())


_SYMBOLS = [
    ("−", " minus "), ("×", " times "), ("÷", " divided by "), ("≥", " is at least "), ("≤", " is at most "),
    ("≠", " is not equal to "), ("≈", " is approximately "), ("°", " degrees"), ("²", " squared"), ("³", " cubed"),
    ("√", " square root of "), ("π", " pi "), ("→", " gives "), ("%", " percent"), ("=", " equals "), ("+", " plus "),
]


def clean_line(text):
    """Remove things that do not belong in a spoken line: markdown, brackets, stage directions, speaker labels."""
    t = re.sub(r"[*_`#>]+", "", text or "")
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"\((?:[^)]*)\)", "", t)
    names = "|".join(re.escape(n) for n in {config.HOST_A_NAME, config.HOST_B_NAME})
    t = re.sub(
        rf"^\s*(?:host\s*[ab]|speaker\s*[ab]|student|teacher|host|expert|narrator|{names})\s*:\s*", "", t, flags=re.I
    )
    t = t.replace("—", ", ").replace("–", ", ").replace("…", "...")
    return re.sub(r"\s+", " ", t).strip()


def speech_safe(text, lang="en"):
    """Last safety net before TTS: spell out symbols a speech engine reads badly.
    The replacements are English words, so they are only applied to English; other languages rely on the
    Script Writer and Speech Director writing symbols out in that language."""
    t = text
    if lang == "en":
        for sym, spoken in _SYMBOLS:
            t = t.replace(sym, spoken)
    return re.sub(r"\s+", " ", t).strip()


# Unicode script ranges -> Bodhan language code (the most common language for that script).
_SCRIPTS = [
    ("hi", 0x0900, 0x097F), ("bn", 0x0980, 0x09FF), ("pa", 0x0A00, 0x0A7F), ("gu", 0x0A80, 0x0AFF),
    ("or", 0x0B00, 0x0B7F), ("ta", 0x0B80, 0x0BFF), ("te", 0x0C00, 0x0C7F), ("kn", 0x0C80, 0x0CFF),
    ("ml", 0x0D00, 0x0D7F), ("ur", 0x0600, 0x06FF), ("sat", 0x1C50, 0x1C7F), ("en", 0x0041, 0x007A),
]


def detect_language(text):
    """Guess the language code from the script the text is written in. Devanagari -> hi, Bengali -> bn."""
    counts = {}
    for ch in text or "":
        o = ord(ch)
        for code, lo, hi in _SCRIPTS:
            if lo <= o <= hi and (code != "en" or ch.isalpha()):
                counts[code] = counts.get(code, 0) + 1
                break
    if not counts:
        return "en"
    return max(counts, key=counts.get)


def split_for_tts(text, limit=None, per_clip=None):
    """Split a speaker's turn for TTS. By default (SENTENCES_PER_CLIP=0) the whole turn is one recording, so it
    sounds like one continuous paragraph; only turns longer than TTS_CHUNK_CHARS are split at sentence ends.
    Very long sentences are cut near a comma."""
    limit = limit or config.TTS_CHUNK_CHARS
    per_clip = per_clip or config.SENTENCES_PER_CLIP or 10 ** 6  # 0 = whole turn, split only by length
    sents = [s.strip() for s in re.split(r"(?<=[.!?।॥؟])\s+", text.strip()) if s.strip()]
    chunks, cur, n_cur = [], "", 0
    for s in sents:
        while len(s) > limit:  # a very long sentence: cut at a comma or space near the limit
            cut = max(s.rfind(", ", 0, limit), s.rfind(" ", 0, limit))
            cut = cut if cut > limit // 3 else limit
            piece, s = s[:cut].strip(" ,"), s[cut:].strip(" ,")
            if cur:
                chunks.append(cur)
                cur, n_cur = "", 0
            chunks.append(piece)
        if cur and (len(cur) + 1 + len(s) > limit or n_cur >= per_clip):
            chunks.append(cur)
            cur, n_cur = s, 1
        else:
            cur, n_cur = f"{cur} {s}".strip(), n_cur + 1
    if cur:
        chunks.append(cur)
    return chunks or [text.strip()]


def _tokens(s):
    # letters of any script (vowel signs included), no digits: "10" and "ten" are both fine
    return re.findall(r"[^\W\d_]+(?:[\u0900-\u0D7F]*)", (s or "").lower())


def mismatch(expected, heard):
    """0.0 = identical wording, 1.0 = nothing in common (letters only; numbers can be spelled either way)."""
    a, b = _tokens(expected), _tokens(heard)
    if not a:
        return 0.0
    if not b:
        return 1.0
    return round(1 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)
