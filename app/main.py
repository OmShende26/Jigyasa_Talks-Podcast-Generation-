"""HTTP API (FastAPI). A thin layer: it validates input and calls the store, documents and orchestrator.
The whole interface is static/index.html, served at /.
"""
import json
"""HTTP API. Thin: validation and routing only; the work happens in documents.py and orchestrator.py."""
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import converters, config, documents, i18n, options, orchestrator, producer, store
from .agents import registry

app = FastAPI(title="Podcast Generation")


class MessageBody(BaseModel):
    mode: str = "full"  # "full" = whole podcast again; "part" = a podcast on one specific part/topic
    text: str = ""
    settings: Optional[dict] = None


class RegenBody(BaseModel):
    note: str = ""  # "Ask AI to revise": what to change


class EditBody(BaseModel):
    lines: List[str]  # "Edit the script myself": the new text of every line, in order


class DocLanguageBody(BaseModel):
    language: str


def _session(sid):
    if not store.valid_id(sid):
        raise HTTPException(404, "Unknown session")
    try:
        return store.get(sid)
    except KeyError:
        raise HTTPException(404, "Unknown session")


def _keys_missing():
    return [n for n, v in (("OPENROUTER_API_KEY", config.OPENROUTER_API_KEY), ("BODHAN_API_KEY", config.BODHAN_API_KEY)) if not v]


@app.get("/")
def index():
    """Serve the single-page interface (never cached, so updates show on reload)."""
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/health")
def health():
    """Server status: keys present, models, music files, parallel slots."""
    return {
        "openrouter_key": bool(config.OPENROUTER_API_KEY), "bodhan_key": bool(config.BODHAN_API_KEY),
        "bodhan_translate_key": bool(config.BODHAN_TRANSLATE_API_KEY),
        "models": registry.all_models(), "voices": {"A": config.HOST_A_VOICE, "B": config.HOST_B_VOICE},
        "audio_check": producer.asr_active(), "parallel": orchestrator.slots.stats(),
        "music": {"folder": str(config.ASSETS_DIR), "intro": str(config.music("intro") or ""), "outro": str(config.music("outro") or "")},
    }


@app.get("/api/ui/{lang}")
def ui_strings(lang: str):
    """Interface text in `lang` (pre-saved in static/i18n/, see app/build_ui_text.py)."""
    return i18n.strings(lang)


@app.get("/api/options")
def get_options():
    """Every choice the interface offers (from options.yaml) and the interface languages."""
    return {**options.payload(), "ui_languages": i18n.available()}


# --- sessions ---

@app.get("/api/sessions")
def list_sessions():
    """All podcast spaces, newest first."""
    return store.list_sessions()


@app.post("/api/sessions")
def create_session():
    """Create an empty podcast space."""
    return {"id": store.create(options.normalize({}))}


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    """Full state of one podcast space: documents, requests, podcasts, log."""
    return _session(sid)


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    """Delete a podcast space with its files and audio."""
    st = _session(sid)
    if any(d["status"] == "analyzing" for d in st["documents"]) or any(
        e["status"] in store.EP_RUNNING or any(s["status"] in store.SEG_BUSY for s in e.get("segments", [])) for e in st["episodes"]
    ):
        raise HTTPException(409, "Wait until the work in this session has finished")
    store.delete(sid)
    return {"ok": True}


# --- documents ---

@app.post("/api/sessions/{sid}/documents")
async def add_documents(sid: str, files: List[UploadFile] = File(...)):
    """Upload files (PDF, image, Word, PowerPoint, text); each is converted and read in the background."""
    st = _session(sid)
    if len(st["documents"]) + len(files) > config.MAX_DOCS_PER_SESSION:
        raise HTTPException(400, f"A session can hold up to {config.MAX_DOCS_PER_SESSION} PDFs")
    payloads = []
    for f in files:
        data = await f.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"{f.filename} is larger than {config.MAX_UPLOAD_MB} MB")
        try:
            pdf, text_pages, kind = converters.to_pdf(f.filename or "document.pdf", data)
        except converters.UnsupportedFile as e:
            raise HTTPException(400, str(e))
        payloads.append((f.filename or "document.pdf", pdf, text_pages, kind))
    ids = []
    for name, data, text_pages, kind in payloads:
        def add(st, name=name, kind=kind):
            did = store.next_id(st["documents"], "d")
            st["documents"].append({"id": did, "filename": name, "kind": kind, "status": "analyzing", "error": None, "pages": None, "info": {}})
            return did
        did = store.update(sid, add)
        ddir = store.session_dir(sid) / "docs" / did
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / "source.pdf").write_bytes(data)
        if text_pages:  # Word / PowerPoint / text: the original text, used instead of re-reading the PDF
            (ddir / "text.json").write_text(json.dumps(text_pages, ensure_ascii=False), encoding="utf-8")
        documents.analyze(sid, did)
        ids.append(did)
    return {"ids": ids}


# --- messages (each one becomes a podcast) ---

@app.post("/api/sessions/{sid}/messages")
def send_message(sid: str, body: MessageBody):
    """A request from the composer: starts a new podcast in the background."""
    st = _session(sid)
    missing = _keys_missing()
    if missing:
        raise HTTPException(400, f"Missing {', '.join(missing)} in .env. See README.")
    if body.mode not in ("full", "part"):
        raise HTTPException(400, "mode must be 'full' or 'part'")
    if body.mode == "part" and not body.text.strip():
        raise HTTPException(400, "Say which part or topic you want the podcast about")
    if not st["documents"]:
        raise HTTPException(400, "Add a PDF to this session first")
    lang = (body.settings or {}).get("language") or st["settings"].get("language")
    if lang and lang != "auto" and lang in options.table()["languages"] and options.write_language(lang) != lang and not config.BODHAN_TRANSLATE_API_KEY:
        raise HTTPException(400, f"{options.language_name(lang)} needs Bodhan translation: add BODHAN_TRANSLATE_API_KEY to .env and restart")
    if all(d["status"] == "error" for d in st["documents"]):
        raise HTTPException(400, "None of the PDFs in this session could be read")
    try:
        settings = options.normalize(body.settings or {}, base=st["settings"])
    except ValueError as e:
        raise HTTPException(400, str(e))

    def add(st):
        mid, eid = store.next_id(st["messages"], "m"), store.next_id(st["episodes"], "e")
        st["messages"].append({"id": mid, "role": "user", "mode": body.mode, "text": body.text.strip()[:2000],
                               "settings": settings, "episode": eid})
        st["episodes"].append({
            "id": eid, "number": len(st["episodes"]) + 1, "message": mid, "mode": body.mode,
            "request": body.text.strip()[:2000], "requested_settings": settings, "settings": settings,
            "roles": options.roles(settings["format"]), "status": "queued", "error": None,
            "understanding": "", "needs": [], "scope_text": "", "title": "", "segments": [], "audio": None,
        })
        st["settings"] = settings  # remembered as this session's starting choice, nowhere else
        return eid
    eid = store.update(sid, add)
    orchestrator.start(sid, eid)
    return {"episode": eid}


# --- segments ---

@app.post("/api/sessions/{sid}/episodes/{eid}/segments/{idx}/regenerate")
def regenerate(sid: str, eid: str, idx: int, body: RegenBody):
    """'Revise with AI' (note given) or 'Make a new version' (empty note) for one segment."""
    _session(sid)
    try:
        orchestrator.regenerate(sid, eid, idx, body.note)
    except IndexError:
        raise HTTPException(404, "No such segment")
    except orchestrator.Busy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/sessions/{sid}/episodes/{eid}/segments/{idx}/edit")
def edit_segment(sid: str, eid: str, idx: int, body: EditBody):
    """Manual edit: the edited lines are re-recorded with TTS; no agents, no translation."""
    st = _session(sid)
    if not body.lines or any(not str(x).strip() for x in body.lines):
        raise HTTPException(400, "Every line needs some text")
    ep = store.find(st["episodes"], eid)
    if not ep or not 0 <= idx < len(ep.get("segments") or []):
        raise HTTPException(404, "No such segment")
    seg = ep["segments"][idx]
    cur = next((v for v in seg["versions"] if v["v"] == seg.get("current_v")), None)
    if cur and len(body.lines) != len(cur["turns"]):
        raise HTTPException(400, f"The script has {len(cur['turns'])} lines; send exactly that many")
    try:
        orchestrator.regenerate(sid, eid, idx, lines=[str(x)[:2000] for x in body.lines])
    except IndexError:
        raise HTTPException(404, "No such segment")
    except orchestrator.Busy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/sessions/{sid}/documents/{did}/language")
def set_document_language(sid: str, did: str, body: DocLanguageBody):
    """Correct a PDF's language; it is read again (with OCR + translation if the LLMs cannot read it)."""
    st = _session(sid)
    d = store.find(st["documents"], did)
    if not d:
        raise HTTPException(404, "No such document")
    if body.language not in options.table()["languages"]:
        raise HTTPException(400, f"Unknown language: {body.language}")
    if d["status"] == "analyzing":
        raise HTTPException(409, "This PDF is still being read")
    if body.language == d.get("language") and d["status"] == "ready":  # "Yes, that's right": nothing to re-read
        store.update(sid, lambda s: store.find(s["documents"], did).update(language_set=True))
        return {"ok": True, "confirmed": True}
    if any(e["status"] in store.EP_RUNNING for e in st["episodes"]):
        raise HTTPException(409, "Wait until the podcast being made has finished")
    if not options.llm_reads(body.language) and not (config.BODHAN_TRANSLATE_API_KEY and config.BODHAN_OCR_API_KEY):
        raise HTTPException(400, f"{options.language_name(body.language)} PDFs are read with Bodhan OCR and translation: "
                                 "add BODHAN_TRANSLATE_API_KEY (and BODHAN_OCR_API_KEY if OCR uses another key) to .env")
    store.update(sid, lambda s: store.find(s["documents"], did).update(
        language=body.language, language_set=True, status="analyzing", error=None))
    documents.analyze(sid, did)
    return {"ok": True}


@app.post("/api/sessions/{sid}/episodes/{eid}/segments/{idx}/select/{v}")
def select_version(sid: str, eid: str, idx: int, v: int):
    """Make an earlier version of a segment the current one and rebuild the episode."""
    st = _session(sid)
    ep = store.find(st["episodes"], eid)
    if not ep or not 0 <= idx < len(ep["segments"]):
        raise HTTPException(404, "No such segment")
    try:
        orchestrator.select_version(sid, eid, idx, v)
    except KeyError:
        raise HTTPException(404, "No such version")
    except orchestrator.Busy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


app.mount("/files", StaticFiles(directory=config.SESSIONS_DIR), name="files")


def _music_banner():
    for kind in ("intro", "outro"):
        f = config.music(kind)
        print(f"[music] {kind}: {f}" if f else f"[music] {kind}: NOT FOUND - put {kind}.mp3 in {config.ASSETS_DIR}", flush=True)


_music_banner()
