"""How many seconds of finished audio one script word turns into, per podcast language - MEASURED.

The word budget for a podcast comes from this rate. A fixed guess is wrong for translated languages (a Tamil or
Konkani line is longer than the English line it came from) and for each voice's speed, which is why short podcasts
came out far too long. So after every recorded segment the real duration is compared with the number of script
words (the checked English words for translated languages) and a running average is kept per language in
data/speech_rates.json. Until a language has been measured, a starting value is used (TTS_WPM, +30% for
translated languages).
"""
import json
import threading

from . import config, options

_FILE = config.SESSIONS_DIR.parent / "speech_rates.json"
_lock = threading.Lock()
ALPHA = 0.35  # weight of the newest measurement


def _load():
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default(lang):
    """Starting seconds-per-word for a language before it has been measured."""
    pause = (config.PAUSE_TURN_MIN_MS + config.PAUSE_TURN_MAX_MS) / 2000.0 / 40  # ~one speaker change per 40 words
    base = 60.0 / config.TTS_WPM + pause
    return base * (1.3 if options.write_language(lang) != lang else 1.0)


def seconds_per_word(lang):
    """Measured seconds of audio per script word for a language (or the default)."""
    r = _load().get(lang)
    return r["rate"] if r and r.get("n") else default(lang)


def record(lang, seconds, words):
    """Add one measurement (a finished segment)."""
    if words < 30 or seconds <= 0:
        return
    obs = max(0.25, min(1.5, seconds / words))
    with _lock:
        data = _load()
        r = data.get(lang) or {"rate": default(lang), "n": 0}
        r["rate"] = obs if r["n"] == 0 else (1 - ALPHA) * r["rate"] + ALPHA * obs
        r["n"] += 1
        data[lang] = r
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
