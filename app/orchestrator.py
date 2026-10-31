"""Runs a session request end to end:

    wait for PDFs -> Request Interpreter -> scope -> Learning Designer -> segments (in parallel) -> joined episode

Memory: the interpreter sees only THIS session's documents and earlier requests. A new session starts empty.
Concurrency: at most config.MAX_PARALLEL segments are produced at once across all sessions (hard cap 4);
the first run of an episode uses one slot fewer, so "Regenerate" always has a slot free.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from . import config, instructions, options, producer, store
from .agents import learning_designer, request_interpreter
from .speech import audio


class Busy(Exception):
    pass


class _Slots:
    def __init__(self, n):
        self.limit, self._sem, self._lock, self.active, self.peak = n, threading.BoundedSemaphore(n), threading.Lock(), 0, 0

    @contextmanager
    def run(self):
        self._sem.acquire()
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            yield
        finally:
            with self._lock:
                self.active -= 1
            self._sem.release()

    def stats(self):
        with self._lock:
            return {"limit": self.limit, "active": self.active, "peak": self.peak}


slots = _Slots(config.MAX_PARALLEL)
_guard = threading.Lock()
_busy = set()
_queued = 0
_ep_locks = {}


# --------------------------------------------------------------------------- helpers

def _ep_update(sid, eid, **kw):
    store.update(sid, lambda st: store.find(st["episodes"], eid).update(kw))


def _seg_update(sid, eid, idx, **kw):
    store.update(sid, lambda st: store.find(st["episodes"], eid)["segments"][idx].update(kw))


def _emitter(sid, eid):
    return lambda m: store.log(sid, m, eid)


def ep_dir(sid, eid):
    """Folder of one podcast (episode) inside its session."""
    return store.session_dir(sid) / "episodes" / eid


def _ready_docs(st):
    return [d for d in st["documents"] if d["status"] == "ready"]


def _blocks_by_doc(sid, docs):
    return {d["id"]: store.load_blocks(sid, d["id"]) for d in docs}


def _blocks_for(sid, ids):
    by_doc = {}
    for i in ids:
        by_doc.setdefault(i.split("p")[0], []).append(i)
    out = {}
    for did in by_doc:
        for b in store.load_blocks(sid, did):
            out[b["id"]] = b
    return {i: out[i] for i in ids if i in out}


def _history(st, eid):
    """What the interpreter is told about earlier podcasts in THIS session."""
    lines = []
    for e in st["episodes"]:
        if e["id"] == eid or e["status"] != "done":
            continue
        lines.append(
            f"- Podcast {e['number']} ({'whole' if e['mode'] == 'full' else 'specific part'}, {options.labels(e['settings'])}): "
            f"\"{e.get('title', '')}\". Covered: {e.get('scope_text', '')}. Request: \"{e['request'] or '(none)'}\". "
            f"Needs: {'; '.join(e.get('needs') or []) or 'none'}."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- episode run

def start(sid, eid):
    """Start making a podcast for a new request, in a background thread."""
    threading.Thread(target=_run, args=(sid, eid), daemon=True, name=f"ep-{sid}-{eid}").start()


def _run(sid, eid):
    emit = _emitter(sid, eid)
    try:
        _ep_update(sid, eid, status="waiting")
        t0 = time.time()
        while True:
            st = store.get(sid)
            if not any(d["status"] == "analyzing" for d in st["documents"]):
                break
            if time.time() - t0 > 1800:
                raise RuntimeError("Reading the PDFs took too long")
            time.sleep(1)
        docs = _ready_docs(st)
        if not docs:
            raise RuntimeError("There is no readable PDF in this session yet. Add a PDF first.")
        ep = store.find(st["episodes"], eid)
        by_doc = _blocks_by_doc(sid, docs)

        _ep_update(sid, eid, status="interpreting")
        emit("Request Interpreter: working out what you asked for")
        req = instructions.for_agents(ep["request"], emit, "your request")
        try:
            it = request_interpreter.interpret(ep["mode"], req, ep["requested_settings"], docs, by_doc, _history(st, eid))
        except Exception as e:
            emit(f"Request Interpreter failed ({e}); using your message as written")
            it = {"found": True, "understanding": "", "whole": ep["mode"] == "full", "ranges": [], "topics": [req] if req else [],
                  "needs": [req] if req else [], "settings_changes": {}, "title_hint": ""}
        settings = options.normalize(it["settings_changes"], base=ep["requested_settings"], strict=False)
        if settings.get("depth") == "custom" and instructions.needs_translation(settings.get("depth_note", "")):
            settings["depth_note_en"] = instructions.for_agents(settings["depth_note"], emit, "the level of detail you described")
        blocks, scope_text = request_interpreter.resolve_scope(it, ep["mode"], docs, by_doc)
        understanding = it["understanding"] or ("A new version of the whole podcast." if ep["mode"] == "full" else "A podcast on the part you asked about.")
        _ep_update(sid, eid, understanding=understanding, needs=it["needs"], settings=settings,
                   roles=options.roles(settings["format"]), scope_text=scope_text, topics=it["topics"])
        if not it["found"] or not blocks:
            _ep_update(sid, eid, status="not_found",
                       error=understanding if not it["found"] else "I could not find that part in the uploaded PDFs. Try naming the page, heading or topic.")
            emit("Nothing in the PDFs matched the request")
            return
        lang, from_pdf = options.resolve_language(settings, blocks, {d["id"]: d.get("language") for d in docs})
        settings = {**settings, "language": lang}
        if options.write_language(lang) != lang and not config.BODHAN_TRANSLATE_API_KEY:
            raise RuntimeError(f"{options.language_name(lang)} needs Bodhan translation: add BODHAN_TRANSLATE_API_KEY to .env and restart the server")
        _ep_update(sid, eid, settings=settings, language_from_pdf=from_pdf)
        emit(f"Scope: {scope_text} ({len(blocks)} blocks); language: {options.language_name(lang)}"
             + (" (same as the PDF)" if from_pdf else "")
             + (f"; written in {options.language_name(options.write_language(lang))}, then translated by Bodhan"
                if options.write_language(lang) != lang else ""))

        _ep_update(sid, eid, status="planning")
        title, segs = learning_designer.plan(blocks, settings, it["needs"], emit,
                                             doc_names={d["id"]: d["filename"] for d in docs})
        segments = []
        for i, s in enumerate(segs):
            bl = [b for b in blocks if b["id"] in set(s["block_ids"])]
            pages = []
            for b in bl:
                if (b["doc"], b["page"]) not in pages:
                    pages.append((b["doc"], b["page"]))
            segments.append({**s, "idx": i, "pages": [{"doc": d, "page": p} for d, p in pages],
                             "status": "pending", "error": None, "current_v": None, "versions": []})
        title = it["title_hint"] if (title == "Podcast" and it["title_hint"]) else title
        _ep_update(sid, eid, title=title, segments=segments, status="writing")
        if lang != "en" and config.BODHAN_TRANSLATE_API_KEY:
            _localize(sid, eid, lang, title, understanding, scope_text, segments, emit)

        def job(i):
            with slots.run():
                return _do_segment(sid, eid, i)

        with ThreadPoolExecutor(max_workers=max(1, config.MAX_PARALLEL - 1)) as pool:
            results = list(pool.map(job, range(len(segments))))
        if not any(results):
            first = next((s["error"] for s in store.find(store.get(sid)["episodes"], eid)["segments"] if s.get("error")), "unknown")
            raise RuntimeError(f"Every segment failed. First error: {first}")

        _ep_update(sid, eid, status="assembling")
        rebuild(sid, eid)
        _ep_update(sid, eid, status="done")
        emit("Podcast ready")

        shown = (store.find(store.get(sid)["episodes"], eid).get("display") or {}).get("title") or title

        def retitle(st):
            if st["title"] in ("New session",) or (len(st["episodes"]) == 1 and st["title"] == _first_doc_name(st)):
                st["title"] = shown
        store.update(sid, retitle)
    except Exception as e:
        emit(f"Stopped: {e}")
        _ep_update(sid, eid, status="error", error=str(e)[:400])


def _localize(sid, eid, lang, title, understanding, scope_text, segments, emit):
    """Titles and descriptions shown on screen, in the podcast language (Bodhan translation).
    Stored as ep["display"]; the agents keep using the English originals. Anything that fails stays in English."""
    import re
    from concurrent.futures import ThreadPoolExecutor as _Pool
    from . import translation

    def tr(text):
        if not text or not text.strip():
            return text
        ids = re.findall(r"\bd\d+\b", text)  # PDF ids like d1 are shown as file names: keep them intact
        prot = text
        for i, d in enumerate(ids):
            prot = prot.replace(d, f"[{i + 1}]", 1)
        try:
            out = translation.translate_text(prot, lang)
        except Exception:
            return text
        for i, d in enumerate(ids):
            if f"[{i + 1}]" not in out:
                return text
            out = out.replace(f"[{i + 1}]", d, 1)
        return out

    items = [title, understanding, scope_text] + [s["title"] for s in segments] + [k for s in segments for k in s["key_points"]]
    emit(f"Translator: putting the titles and descriptions into {options.language_name(lang)}")
    with _Pool(max_workers=3) as pool:
        out = list(pool.map(tr, items))
    n = len(segments)
    seg_titles, kps, pos = out[3:3 + n], [], 3 + n
    for s in segments:
        kps.append(out[pos:pos + len(s["key_points"])])
        pos += len(s["key_points"])
    _ep_update(sid, eid, display={"language": lang, "title": out[0], "understanding": out[1], "scope": out[2],
                                  "segments": seg_titles, "key_points": kps})


def _first_doc_name(st):
    import re
    return re.sub(r"\.[A-Za-z0-9]{2,5}$", "", st["documents"][0]["filename"])[:80] if st["documents"] else ""


def _meta(sid, ep):
    contents = [s for s in ep["segments"] if s["kind"] == "content"]
    ids = [i for s in ep["segments"] for i in s["block_ids"]]
    return {
        "title": ep["title"], "total": len(ep["segments"]), "titles": [s["title"] for s in ep["segments"]],
        "settings": ep["settings"], "roles": ep["roles"], "needs": ep.get("needs") or [],
        "outline": [{"title": s["title"], "key_points": s["key_points"]} for s in contents],
        "blocks": _blocks_for(sid, ids),
    }


def _do_segment(sid, eid, idx, note=None, previous=None, lines=None):
    """lines given -> manual edit (TTS only). Otherwise the AI path."""
    emit = _emitter(sid, eid)
    ep = store.find(store.get(sid)["episodes"], eid)
    seg = ep["segments"][idx]
    version = max([x["v"] for x in seg["versions"]], default=0) + 1
    _seg_update(sid, eid, idx, status="working", error=None)
    try:
        if lines is not None:
            v = producer.rerecord(ep_dir(sid, eid), _meta(sid, ep), seg, version, emit, _current(seg), lines)
        else:
            v = producer.produce(ep_dir(sid, eid), _meta(sid, ep), seg, version, emit, note, previous)

        def done(st):
            sg = store.find(st["episodes"], eid)["segments"][idx]
            sg["versions"].append(v)
            sg["current_v"], sg["status"], sg["job"] = v["v"], "done", None
        store.update(sid, done)
        emit(f"Segment {idx + 1} ready (version {version})")
        return True
    except Exception as e:
        emit(f"Segment {idx + 1} failed: {e}")
        _seg_update(sid, eid, idx, status="error", error=str(e)[:300], job=None)
        return False


def _current(seg):
    return next((x for x in seg["versions"] if x["v"] == seg.get("current_v")), None)


def rebuild(sid, eid):
    """Join the current version of every finished segment and record where each segment starts."""
    with _guard:
        lock = _ep_locks.setdefault((sid, eid), threading.Lock())
    with lock:
        ep = store.find(store.get(sid)["episodes"], eid)
        d = ep_dir(sid, eid)
        paths, offsets, t = [], {}, 0.0
        for s in ep["segments"]:
            v = _current(s)
            if not v:
                continue
            if paths:
                t += config.PAUSE_SEGMENT_MS / 1000.0
            offsets[str(s["idx"])] = round(t, 3)
            paths.append(d / v["audio"])
            t += v["duration"]
        if not paths:
            _ep_update(sid, eid, audio=None)
            return
        speech, tmp, out = d / "speech.tmp.mp3", d / "episode.tmp.mp3", d / "episode.mp3"
        audio.concat_mp3(paths, speech, config.PAUSE_SEGMENT_MS)
        complete = len(paths) == len(ep["segments"])
        # music only on the complete podcast: intro before the first segment, outro after the last
        intro_f, outro_f = (config.music("intro"), config.music("outro")) if complete else (None, None)
        if complete:
            found = [f"{k}: {f.name}" for k, f in (("intro", intro_f), ("outro", outro_f)) if f]
            missing = [k for k, f in (("intro", intro_f), ("outro", outro_f)) if not f]
            if missing:
                store.log(sid, f"Music: no {' or '.join(missing)} file found in {config.ASSETS_DIR} "
                               f"(expected {' and '.join(m + '.mp3' for m in missing)}), so it was left out")
            if found:
                store.log(sid, "Music added (" + ", ".join(found) + ")")
        lead = audio.add_music(speech, tmp, intro_f, outro_f,
                               warn=lambda m: (store.log(sid, m), print(f"[music] {m}", flush=True)))
        speech.unlink(missing_ok=True)
        store.safe_replace(tmp, out)
        _ep_update(sid, eid, audio={
            "path": f"episodes/{eid}/episode.mp3", "duration": audio.duration(out), "built": int(time.time() * 1000),
            "offsets": {k: round(v + lead, 3) for k, v in offsets.items()}, "speech_start": lead,
            "music": {"intro": bool(intro_f) and lead > 0, "outro": bool(outro_f)},
            "included": len(paths), "total": len(ep["segments"]),
        })


def regenerate(sid, eid, idx, note="", lines=None):
    """Make a new version of one segment.

    lines=None: "Ask AI to revise" - the agents rewrite it (note = what to change), then TTS.
    lines=[...]: "Edit the script myself" - the edited lines go straight to TTS.
    """
    global _queued
    ep = store.find(store.get(sid)["episodes"], eid)
    if not ep or not 0 <= idx < len(ep.get("segments") or []):
        raise IndexError(idx)
    seg, key = ep["segments"][idx], (sid, eid, idx)
    with _guard:
        if key in _busy or seg["status"] in store.SEG_BUSY:
            raise Busy("This segment has not been made yet." if seg["status"] == "pending" else "This segment is already being worked on.")
        if _queued >= config.MAX_QUEUED_REGENS:
            raise Busy("Too many regenerations are waiting. Try again in a minute.")
        _busy.add(key)
        _queued += 1
    cur = _current(seg)
    if lines is not None and not cur:
        with _guard:
            _busy.discard(key)
            _queued -= 1
        raise Busy("There is no script to edit yet. Use 'Ask AI to revise' first.")
    previous = cur["turns"] if cur else None
    note = (note or "").strip() or None
    _seg_update(sid, eid, idx, status="queued", error=None, job="manual" if lines is not None else "ai")

    def release():
        global _queued
        with _guard:
            if key in _busy:
                _busy.discard(key)
                _queued -= 1

    def work():
        nonlocal note
        try:
            if note:  # "Ask AI to revise" text can be in any language
                note = instructions.for_agents(note, _emitter(sid, eid), "your change request")
            with slots.run():
                _do_segment(sid, eid, idx, note=note, previous=previous, lines=lines)
            release()  # the segment is free again; re-mixing the full podcast (locked per podcast) follows
            rebuild(sid, eid)
        except Exception as e:
            _emitter(sid, eid)(f"Rebuilding the podcast failed: {e}")
        finally:
            release()

    threading.Thread(target=work, daemon=True).start()


def select_version(sid, eid, idx, v):
    """Switch a segment to another version and rebuild the episode audio."""
    ep = store.find(store.get(sid)["episodes"], eid)
    seg = ep["segments"][idx]
    if (sid, eid, idx) in _busy or seg["status"] in store.SEG_BUSY:
        raise Busy("Wait for this segment to finish")
    if not any(x["v"] == v for x in seg["versions"]):
        raise KeyError(v)

    def f(st):
        sg = store.find(st["episodes"], eid)["segments"][idx]
        sg["current_v"], sg["status"] = v, "done"
    store.update(sid, f)
    rebuild(sid, eid)
