"""Offline stand-ins for OpenRouter and Bodhan, so the whole system can be tested without keys.

Every agent goes through app.agents.base.call_model(spec, messages); the fake dispatches on spec.name.
Fakes echo what they were told (settings, needs, how many earlier podcasts) so tests can check the plumbing.

Traps the agents must handle (content segments of a podcast):
  segment 1, first draft: an unsupported claim  -> reviewer sends it back, writer fixes it
  segment 2: a "tongue-twister" misheard on the first take -> audio check records it again
  segment 3: the reviewer never approves         -> flagged for review after the revision rounds
MOCK_DELAY (seconds) slows each fake model call so parallel work overlaps.
"""
import hashlib
import io
import json
import math
import os
import re
import struct
import threading
import time
import wave

from app.agents import base
from app import config
from app.speech import bodhan

_heard, _tts_calls, _lock = {}, {}, threading.Lock()
OCR_TEXT = "Scanned page paragraph one explains how plants store food as starch.\n\nScanned page paragraph two says starch turns blue-black with iodine."
FIGURE_CAPTION = "Fig. 3.1 The water cycle"
FIGURE_DESC = "A diagram of the water cycle: water rises from the sea as vapour, forms clouds, and falls back as rain."
CALLS = {}


# ------------------------------------------------------------------ Bodhan
def _wav(text, voice, take):
    sr, dur = 22050, max(0.4, 0.25 * len(text.split()))
    uniq = int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:4], 16) % 97  # different text -> different audio
    f, n = (440 if voice == "VoiceA" else 330) + 3 * take + uniq, int(sr * dur)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * f * i / sr))) for i in range(n)))
    return buf.getvalue()


def mock_tts(text, voice, style=None, lang=None):
    log = os.environ.get("DATA_DIR")
    if log:  # lets the test check which language code reached Bodhan
        with _lock, open(os.path.join(log, "tts_calls.log"), "a", encoding="utf-8") as f:
            f.write(f"{lang}\t{style}\t{text[:40]}\n")
    with _lock:
        take = _tts_calls[text] = _tts_calls.get(text, 0) + 1
    data = _wav(text, voice, take)
    _heard[hashlib.sha1(data).hexdigest()] = "zzz qqq" if ("tongue-twister" in text and take == 1) else text
    return data


_SCRIPT_CHAR = {"ta": "த", "bn": "ক", "te": "త", "kn": "ಕ", "ml": "ക", "gu": "ક", "pa": "ਕ", "or": "କ", "ur": "ک", "en": "a"}


_translated = {}


def mock_translate(text, target, max_output_tokens=None):
    """Answers in the target script. A long line is reported cut off once, to exercise the sentence-by-sentence path."""
    with _lock:
        CALLS["translate"] = CALLS.get("translate", 0) + 1
    ch = _SCRIPT_CHAR.get(target, "क")
    with _lock:
        seen = _translated[text] = _translated.get(text, 0) + 1
    # keeps [1] placeholders and numbers; the first translation of a line with "21" gets it wrong ("12")
    out = " ".join(("12" if (w.strip(".,") == "21" and seen == 1) else w) if re.fullmatch(r"(\[\d+\]|\d+)\W*", w) else ch * 3
                   for w in text.split())
    truncated = len(re.split(r"(?<=[.!?])\s+", text.strip())) > 1 and "Setting check" in text
    return (out[: len(out) // 2] if truncated else out), truncated


MAITHILI_OCR = "मैथिली पाठ पहिल अनुच्छेद। पौधा अपन भोजन बनबैत अछि।\n\nमैथिली पाठ दोसर अनुच्छेद। जड़ि पानि सोखैत अछि।"


def mock_ocr(png_bytes):
    with _lock:
        CALLS["ocr"] = CALLS.get("ocr", 0) + 1
    assert png_bytes[:4] == b"\x89PNG", "OCR must receive a PNG"
    return MAITHILI_OCR


def mock_asr(wav_bytes, lang=None):
    return _heard[hashlib.sha1(wav_bytes).hexdigest()]


# ------------------------------------------------------------------ agents
def _analyst(system, user):
    text = user.split("from this page:\n", 1)[1]
    out = {"transcription": "", "visuals": []}
    if text.strip() == "(none)":
        out["transcription"] = OCR_TEXT
    if "water cycle" in text.lower():
        out["visuals"].append({"kind": "figure", "caption": FIGURE_CAPTION, "description": FIGURE_DESC})
    return out


def _interpreter(system, user):
    mode = re.search(r"MODE: (\w+)", user).group(1)
    req = user.split("REQUEST: ", 1)[1].strip()
    if "(The student's original words, in " in req:  # like a real model, read the original wording too
        req = req + " " + req.split("original words, in ", 1)[1].split(": ", 1)[1].rstrip(")")
    hist = system.split("Earlier in THIS session")[1].split("Chosen settings:")[0]
    previous = len(re.findall(r"^- Podcast \d+", hist, re.M))
    digest = system.split("Documents and page digest:")[1]
    out = {"found": True, "scope": {"whole": mode == "full", "ranges": [], "topics": []}, "needs": [], "settings_changes": {},
           "title_hint": ""}
    if "teacher" in req.lower():
        out["settings_changes"]["format"] = "teacher_student"
    for word, code in (("in hindi", "hi"), ("हिंदी", "hi"), ("in english", "en"), ("in tamil", "ta")):
        if word in req.lower():
            out["settings_changes"]["language"] = code
    if "simpler" in req.lower():
        out["needs"].append("use simpler words")
    if "(The student's original words, in " in req:
        out["needs"].append("got the English translation and the original words")
    if mode == "part":
        m = re.search(r"about ([^,]+?)(?:,|$)", req.lower())
        topic = (m.group(1) if m else req.lower()).strip()
        out["scope"]["topics"] = [topic]
        doc = None
        hits = {}
        for line in digest.splitlines():
            dm = re.match(r"(d\d+): ", line)
            if dm:
                doc = dm.group(1)
            pm = re.match(r"\s+p(\d+): (.*)", line)
            if pm and topic in pm.group(2).lower():
                hits.setdefault(doc, []).append(int(pm.group(1)))
        for d, pages in hits.items():
            out["scope"]["ranges"].append({"doc": d, "from": min(pages), "to": max(pages)})
        if not hits:
            out["found"] = False
        out["title_hint"] = topic.title()
    out["understanding"] = f"previous={previous}; mode={mode}; I will make it."
    return out


def _designer(system, user):
    n = int(re.search(r"Make exactly (\d+) segments", system).group(1))
    ids = re.findall(r"^\[(d\d+p\d+b\d+)\]", user, re.M)
    step = max(1, len(ids) // n)
    segs = []
    for i in range(n):
        a = i * step
        b = len(ids) - 1 if i == n - 1 else (i + 1) * step - 1
        segs.append({"title": f"Topic {i + 1}", "first_block": ids[a], "last_block": ids[b], "key_points": [f"Key idea {i + 1}a", f"Key idea {i + 1}b"]})
    found = {"title": "The basics", "key_points": ["What a plant is", "Why living things need food"]} if 'ALSO give "foundations"' in system else None
    return {"episode_title": "How plants make and store food", "foundations": found, "segments": segs}


def _writer(system, user):
    fmt = re.search(r"FORMAT: ([A-Z ]+)\n", system).group(1).lower()
    subject = re.search(r"SUBJECT: ([A-Z ]+)[ .(]", system).group(1).strip().lower()
    depth = re.search(r"DETAIL: ([A-Z ]+?)[.:]", system).group(1).lower()
    age = "primary" if "AUDIENCE: CHILDREN IN PRIMARY" in system else "secondary"
    target = int(re.search(r"about (\d+) words", system).group(1))
    solo = "Use ONLY speaker A" in system
    idx = int(re.search(r"SEGMENT (\d+) of", user).group(1)) - 1
    language = re.search(r"LANGUAGE: Write every spoken line in (\w+)", system).group(1).lower()
    lines = [(f"Setting check: {fmt}, {subject}, {depth}, {age}, budget {target} words. Language {language}.", [])]
    names = re.search(r'speaker "A" is (\w+)', system).group(1)
    assert "do NOT greet" in system or "START OF THE PODCAST" not in system
    if "THE STUDENT'S REQUEST" in user:
        need = re.search(r"THE STUDENT'S REQUEST FOR THIS PODCAST:\n- (.+)", user).group(1)
        lines.append((f"Request heard: {need}.", []))
    if "KIND: FOUNDATIONS" in user:
        lines += [("This part explains background knowledge from the ground up.", []), ("First, what a plant is.", []), ("Then why living things need food.", [])]
    else:
        src = user.split("SOURCE (the only allowed source of facts):\n")[1].split("\n\nPREVIOUS DRAFT")[0].split("\n\nA FACT-CHECKER")[0].split("\n\nREVIEWER INSTRUCTION")[0].split("\n\nWrite the complete")[0]
        blocks = re.findall(r"^\[(d\d+p\d+b\d+)\] (.+)$", src, re.M)
        lines.append(("So today we start with something simple.", []))
        for bid, text in [b for b in blocks if not b[1].startswith("[")][:4]:
            lines.append(("Right, " + re.split(r"(?<=[.!?])\s", text)[0], [bid]))
        for bid, text in blocks:
            if text.startswith("[Figure"):
                lines.append(("Picture this: " + text.split("] ", 1)[1][:80], [bid]))
        if idx == 0 and "FACT-CHECKER FOUND" not in user:
            lines.append(("Also the moon is made of cheese UNSUPPORTED.", []))
        if idx == 0:
            lines.append(("Leaves have 21 tiny pores in this example.", []))
            lines.append(("Forests cover 10 lakh square km in this example.", []))
        if idx == 1:
            lines.append(("Remember that water is H2O.", []))
            lines.append(("Try saying this tongue-twister clearly.", []))
        if idx == 2:
            lines.append(("This line keeps the critic unhappy STUBBORN.", []))
    if "REVIEWER INSTRUCTION" in user:
        lines.append(("As you asked, here is a simpler explanation.", []))
    return {"turns": [{"speaker": "A" if solo else "AB"[i % 2], "text": t, "refs": r} for i, (t, r) in enumerate(lines)]}


def _framer(system, user):
    name = re.search(r'Speaker "A" is (\w+)', system).group(1)
    solo = "Use only speaker A" in system
    teacher = "TEACHER AND STUDENT" in system
    if "write ONLY the opening" in system:
        if teacher:
            return {"turns": [{"speaker": "B", "text": "Good morning, students! Today we are going to learn about plants."},
                              {"speaker": "A", "text": f"Good morning! I am {name}."}]}
        return {"turns": [{"speaker": "A", "text": f"Hello, I am {name} and today's topic is plants."}]}
    return {"turns": [{"speaker": "A" if solo else "B", "text": "That is all for today. Happy learning, and see you again next time."}]}


def _uitrans(system, user):
    assert "word for word" in system and "Open player" in system
    items = json.loads(user)
    return {k: "தமிழ் " + " ".join(re.findall(r"\{\w+\}", v)) + " உரை" for k, v in items.items()}


def _final(system, user):
    issues = []
    for n, a, sp in re.findall(r"LINE (\d+)\nAPPROVED: (.*)\nSPOKEN: (.*)", user):
        if "21" in a and "21" not in sp:
            issues.append({"line": int(n), "type": "number", "detail": "says 12 instead of 21", "approved_value": 21, "spoken_value": 12})
        if "lakh" in a:  # a checker that confuses lakh with million, but gives equal values: must be ignored
            issues.append({"line": int(n), "type": "number", "detail": "10 lakh is not 1 million", "approved_value": "1,000,000", "spoken_value": 1000000})
    return {"issues": issues}


def _reviewer(system, user):
    out = _reviewer0(system, user)
    if "THE STUDENT ASKED FOR THIS CHANGE" in system:
        done = "As you asked" in user.split("SCRIPT:")[1]
        out.update({"request_done": "yes" if done else "no", "request_note": "Added a simpler explanation." if done else "Not in the source."})
    return out


def _reviewer0(system, user):
    script = user.split("SCRIPT:")[1]
    if "UNSUPPORTED" in script:
        return {"faithful": False, "covers_key_points": True, "sounds_natural": True,
                "issues": [{"turn": 5, "type": "unsupported", "detail": "Moon claim is not in the source", "fix": "Remove it"}]}
    if "STUBBORN" in script:
        return {"faithful": True, "covers_key_points": True, "sounds_natural": False,
                "issues": [{"turn": 6, "type": "style", "detail": "Line sounds like a textbook", "fix": "Make it conversational"}]}
    return {"faithful": True, "covers_key_points": True, "sounds_natural": True, "issues": []}


def _director(system, user):
    out = []
    for m in re.finditer(r"^(\d+)\. [AB]: (.+)$", user, re.M):
        i, text = int(m.group(1)), m.group(2)
        d = {"turn": i, "delivery": "curious" if text.endswith("?") else "calm" if "H2O" in text else "explaining"}
        if "H2O" in text:
            d["spoken"] = text.replace("H2O", "H two O")
        if text.startswith("Setting check"):
            d["spoken"] = "Something entirely unrelated that should be rejected by the guard."
        out.append(d)
    return {"turns": out}


def _audience(system, user):
    """Kids: friendlier wording on lines that start with "Right,". Teens: unchanged."""
    kids = "CHILDREN IN PRIMARY" in system
    out = []
    for m in re.finditer(r"^(\d+)\. [AB]: (.+)$", user, re.M):
        text = m.group(2)
        if kids and text.startswith("Right, "):
            text = "Okay friends, " + text[len("Right, "):]
        out.append({"turn": int(m.group(1)), "text": text})
    return {"turns": out}


HANDLERS = {"audience_editor": _audience, "document_analyst": _analyst, "request_interpreter": _interpreter, "learning_designer": _designer,
            "script_writer": _writer, "host_framer": _framer, "final_checker": _final, "ui_translator": _uitrans, "reviewer": _reviewer, "speech_director": _director}


def mock_call_model(spec, messages):
    system = messages[0]["content"]
    content = messages[-1]["content"]
    if isinstance(content, list):
        assert spec.vision, f"{spec.name} got images but is not a vision agent"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        user = content[0]["text"]
    else:
        user = content
    with _lock:
        CALLS[spec.name] = CALLS.get(spec.name, 0) + 1
    if spec.name != "document_analyst":
        time.sleep(float(os.environ.get("MOCK_DELAY", "0")))
    return json.dumps(HANDLERS[spec.name](system, user))


def install():
    base.call_model = mock_call_model
    bodhan.tts, bodhan.asr, bodhan.translate, bodhan.ocr = mock_tts, mock_asr, mock_translate, mock_ocr
    config.OPENROUTER_API_KEY = config.BODHAN_API_KEY = config.BODHAN_TRANSLATE_API_KEY = config.BODHAN_OCR_API_KEY = "mock"
    config.HOST_A_VOICE, config.HOST_B_VOICE = "VoiceA", "VoiceB"
