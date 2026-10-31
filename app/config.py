"""Paths, keys and limits. Everything is overridable from the .env file.

Agent models and prompts are NOT here: see config/agents.yaml and prompts/.
User-facing choices are in config/options.yaml.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name, default=""):
    """Environment value, treating a blank entry in .env the same as an unset one."""
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


def _num(name, default, cast=int, lo=None, hi=None):
    try:
        v = cast(_env(name, str(default)))
    except ValueError:
        v = cast(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# --- Paths ---
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
STATIC_DIR = ROOT / "static"
SESSIONS_DIR = Path(_env("DATA_DIR", str(ROOT / "data"))) / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# --- API keys (never hardcode; put them in .env) ---
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
BODHAN_API_KEY = _env("BODHAN_API_KEY")  # text-to-speech (and speech-to-text)
BODHAN_TRANSLATE_API_KEY = _env("BODHAN_TRANSLATE_API_KEY")  # translation; a separate key
BODHAN_OCR_API_KEY = _env("BODHAN_OCR_API_KEY") or BODHAN_API_KEY  # OCR (indic-ocr); falls back to BODHAN_API_KEY
OCR_MODEL = _env("OCR_MODEL", "indic-ocr")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
BODHAN_BASE = _env("BODHAN_BASE", "https://api.bodhan.ai/v1")
BODHAN_TRANSLATE_MODEL = _env("BODHAN_TRANSLATE_MODEL", "indic-translate")  # via /v1/chat/completions
BODHAN_TRANSLATE_URL = _env("BODHAN_TRANSLATE_URL")  # optional: a REST /translate address, used instead if set

# --- Bodhan models ---
TTS_MODEL = _env("TTS_MODEL", "indic-speak")
ASR_MODEL = _env("ASR_MODEL", "indic-transcribe")
LANG = "en"

# --- Voices (two DIFFERENT voices so listeners can tell the speakers apart) ---
HOST_A_NAME = _env("HOST_A_NAME", "Aarav")
HOST_B_NAME = _env("HOST_B_NAME", "Meera")
HOST_A_VOICE = _env("HOST_A_VOICE", "Amit")
HOST_B_VOICE = _env("HOST_B_VOICE", "Amit")

# --- Agent loops ---
MAX_SEGMENTS = _num("MAX_SEGMENTS", 8, lo=1, hi=12)
MAX_REVISIONS = _num("MAX_REVISIONS", 2, lo=0, hi=4)  # writer <-> reviewer rounds after the first draft
QA_ENABLED = _env("QA_ENABLED", "1") == "1"  # TTS -> ASR check; switches itself off if the key cannot use ASR
TTS_RETRIES = _num("TTS_RETRIES", 1, lo=0, hi=3)
MISMATCH_THRESHOLD = _num("MISMATCH_THRESHOLD", 0.30, cast=float, lo=0.05, hi=0.95)

# --- Parallel work: segments being produced at once across ALL sessions (hard cap 4) ---
MAX_PARALLEL = _num("MAX_PARALLEL", 3, lo=1, hi=4)
MAX_QUEUED_REGENS = 8

# --- Documents ---
MAX_DOCS_PER_SESSION = _num("MAX_DOCS_PER_SESSION", 10, lo=1)
MAX_UPLOAD_MB = _num("MAX_UPLOAD_MB", 50, lo=1)
MAX_PDF_PAGES = _num("MAX_PDF_PAGES", 80, lo=1)
MAX_VISION_PAGES = _num("MAX_VISION_PAGES", 40, lo=0)  # pages per PDF sent to the Document Analyst
ANALYST_WORKERS = _num("ANALYST_WORKERS", 4, lo=1, hi=8)
PAGE_DPI = _num("PAGE_DPI", 110, lo=60, hi=200)  # page images for the viewer and the Document Analyst

# --- Bodhan politeness ---
BODHAN_MIN_INTERVAL = _num("BODHAN_MIN_INTERVAL", 0.5, cast=float, lo=0.0)
TTS_CHUNK_CHARS = _num("TTS_CHUNK_CHARS", 600, lo=80)  # longer turns are split at sentence ends below this size

# --- Audio ---
# Pauses are exact: Bodhan's own leading/trailing silence is trimmed from every clip first.
PAUSE_TURN_MIN_MS = _num("PAUSE_TURN_MIN_MS", 850, lo=0)      # gap between two speakers, varied in this range
PAUSE_TURN_MAX_MS = _num("PAUSE_TURN_MAX_MS", 1000, lo=0)
FINAL_CHECK = _env("FINAL_CHECK", "1") == "1"  # check numbers/facts in the spoken lines before TTS
LENGTH_TOLERANCE = _num("LENGTH_TOLERANCE", 0.15, cast=float, lo=0.0)  # how far over its word budget a segment may run
TTS_WPM = _num("TTS_WPM", 140, lo=60, hi=250)  # how fast the voices speak (words per minute); used to fit the length
PAUSE_SENTENCE_MS = _num("PAUSE_SENTENCE_MS", 300, lo=0)      # gap between sentences of the same speaker
SENTENCES_PER_CLIP = _num("SENTENCES_PER_CLIP", 0, lo=0, hi=20)  # 0 = a speaker's whole turn in one recording
TRIM_SILENCE = _env("TRIM_SILENCE", "1") == "1"
PAUSE_SEGMENT_MS = _num("PAUSE_SEGMENT_MS", 700, lo=0)

# --- Intro / outro music (put your files in assets/; leave them out for no music) ---
ASSETS_DIR = ROOT / "assets"
_AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".wma")


def music(kind):
    """The intro/outro file: INTRO_MUSIC / OUTRO_MUSIC from .env, else assets/<kind>.<any audio type>
    (also finds names like 'intro.mp3.mp3' that Windows creates when file extensions are hidden). None if absent."""
    explicit = _env(f"{kind.upper()}_MUSIC", "")
    if explicit:
        p = Path(explicit.strip().strip('"'))
        p = p if p.is_absolute() else ROOT / p
        return p if p.is_file() else None
    if not ASSETS_DIR.is_dir():
        return None
    hits = sorted(f for f in ASSETS_DIR.iterdir()
                  if f.is_file() and f.name.lower().startswith(kind) and f.suffix.lower() in _AUDIO_EXT)
    return hits[0] if hits else None
INTRO_LEAD_SEC = _num("INTRO_LEAD_SEC", 2.0, cast=float, lo=0.0)   # music alone before the first line
INTRO_FADE_SEC = _num("INTRO_FADE_SEC", 2.0, cast=float, lo=0.0)   # music fades out under the first line
OUTRO_OVERLAP_SEC = _num("OUTRO_OVERLAP_SEC", 2.0, cast=float, lo=0.0)  # outro starts this long before the goodbye ends
OUTRO_GAP_SEC = _num("OUTRO_GAP_SEC", 0.6, cast=float, lo=0.0)     # silence between the goodbye and the outro
MUSIC_VOLUME = _num("MUSIC_VOLUME", 0.6, cast=float, lo=0.0, hi=2.0)
