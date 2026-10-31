"""Translator step: turns an approved script into the listener's language with Bodhan's translation API.

Used when config/options.yaml gives a language `via: en` (or `via: hi`): the agents write and fact-check in
that language, and only the approved lines are translated. Each line is checked deterministically:
not empty, not cut off, and written in the target language's script. A cut-off line is translated again
sentence by sentence; a line in the wrong script is retried once; after that the segment fails clearly.
"""
import re

from . import options
from .speech import bodhan
from .text import detect_language

# The script each language is written in -> the code detect_language() returns for that script.
_SCRIPT_FAMILY = {
    "Latin": "en", "Devanagari": "hi", "Bengali": "bn", "Bengali-Assamese": "bn", "Gurmukhi": "pa",
    "Gujarati": "gu", "Odia": "or", "Tamil": "ta", "Telugu": "te", "Kannada": "kn", "Malayalam": "ml",
    "Perso-Arabic": "ur", "Perso-Arabic (Nastaliq)": "ur", "Ol Chiki": "sat",
}


class TranslationError(Exception):
    pass


def _expected_family(code):
    script = (options.table()["languages"].get(code) or {}).get("script", "")
    return _SCRIPT_FAMILY.get(script)  # None = no reliable check for this script (e.g. Meitei Mayek)


def _in_script(text, family):
    if not family:
        return True
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    hits = sum(1 for ch in letters if detect_language(ch) == family)
    return hits / len(letters) >= 0.5


def _one(text, target, family):
    out, truncated = bodhan.translate(text, target)
    if truncated:  # translate sentence by sentence so nothing is lost
        parts = [p for p in re.split(r"(?<=[.!?।॥؟])\s+", text) if p.strip()]
        pieces = []
        for p in parts:
            piece, cut = bodhan.translate(p, target)
            if cut:
                raise TranslationError(f"translation was cut off even for one sentence: {p[:60]}")
            pieces.append(piece.strip())
        out = " ".join(pieces)
    out = " ".join((out or "").split())
    if not out:
        raise TranslationError("translation came back empty")
    if not _in_script(out, family):
        retry, _ = bodhan.translate(text, target)
        retry = " ".join((retry or "").split())
        if not _in_script(retry, family):
            raise TranslationError(f"translation is not in the {options.language_name(target)} script: {retry[:60]}")
        out = retry
    return out


def translate_text(text, target):
    """One piece of text -> target language, with the same checks as script lines."""
    try:
        return _one(text, target, _expected_family(target))
    except (TranslationError, bodhan.BodhanError) as e:
        raise TranslationError(str(e))


def translate_turns(turns, target, emit, label):
    """In place: text_<source> keeps the checked original; text and speech become the translation."""
    family = _expected_family(target)
    emit(f"Translator · {label}: translating {len(turns)} lines into {options.language_name(target)} with Bodhan")
    for i, t in enumerate(turns, 1):
        try:
            # the spoken form already has formulas and symbols written out as words
            translated = _one(t.get("speech") or t["text"], target, family)
        except (TranslationError, bodhan.BodhanError) as e:
            raise TranslationError(f"line {i}: {e}")
        t["original"], t["speech_src"] = t["text"], t.get("speech") or t["text"]
        t["text"] = t["speech"] = translated
    return turns


def retranslate_turn(t, target):
    """Translate one line again, sentence by sentence (used when the final check finds a changed fact)."""
    family = _expected_family(target)
    src = t.get("speech_src") or t.get("original") or t["text"]
    parts = [p for p in re.split(r"(?<=[.!?।॥؟])\s+", src) if p.strip()]
    out = " ".join(_one(p, target, family) for p in parts)
    t["text"] = t["speech"] = out
    return out
