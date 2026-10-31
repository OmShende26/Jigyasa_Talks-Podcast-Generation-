"""File-backed session store. One folder per session; nothing is shared between sessions.

data/sessions/<sid>/
    state.json                      documents, messages, episodes, activity log
    docs/<did>/source.pdf           an uploaded PDF
    docs/<did>/pages/p<n>.png       page images (viewer + Document Analyst)
    docs/<did>/blocks.json          extracted text/figure blocks with page numbers
    episodes/<eid>/audio/*.mp3      segment audio, one file per version
    episodes/<eid>/episode.mp3      the joined podcast
"""
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config

EP_RUNNING = {"queued", "waiting", "interpreting", "planning", "writing", "assembling"}
SEG_BUSY = {"pending", "queued", "working"}
_ID = re.compile(r"^[a-f0-9]{12}$")
_lock = threading.RLock()
_cache = {}
_blocks_cache = {}


def valid_id(sid):
    """True if the string looks like a session id."""
    return bool(_ID.match(sid or ""))


def session_dir(sid) -> Path:
    """Folder of one session."""
    return config.SESSIONS_DIR / sid


def _path(sid):
    return session_dir(sid) / "state.json"


def safe_replace(src, dst, tries=40):
    """os.replace that survives Windows briefly locking the target (antivirus, OneDrive, search indexer,
    a media player): retries for about 4 seconds, then copies the content over instead."""
    src, dst = Path(src), Path(dst)
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(0.05 + 0.005 * i)
    shutil.copyfile(src, dst)  # last resort: overwrite in place (works when the lock allows writing)
    try:
        src.unlink()
    except OSError:
        pass


def _save(sid, st):
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = _path(sid).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    safe_replace(tmp, _path(sid))


def _load(sid):
    if sid in _cache:
        return _cache[sid]
    p = _path(sid)
    if not p.exists():
        raise KeyError(sid)
    st = json.loads(p.read_text(encoding="utf-8"))
    # Work that was running when the server stopped can never finish: say so instead of spinning forever.
    for d in st.get("documents", []):
        if d.get("status") == "analyzing":
            d["status"], d["error"] = "error", "The server restarted while reading this PDF. Upload it again."
    for e in st.get("episodes", []):
        if e.get("status") in EP_RUNNING:
            e["status"], e["error"] = "error", "The server restarted while this was being made. Send the request again."
        for s in e.get("segments", []):
            if s.get("status") in SEG_BUSY:
                s["status"], s["error"] = "error", "Interrupted by a server restart. Use Regenerate."
    _cache[sid] = st
    return st


def create(settings):
    """Create a new session and return its id."""
    sid = uuid.uuid4().hex[:12]
    session_dir(sid).mkdir(parents=True)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    st = {"id": sid, "title": "New session", "created": now, "updated": now, "settings": settings,
          "documents": [], "messages": [], "episodes": [], "log": []}
    with _lock:
        _cache[sid] = st
        _save(sid, st)
    return sid


def get(sid):
    """Snapshot copy, safe to read without holding the lock."""
    with _lock:
        return json.loads(json.dumps(_load(sid)))


def update(sid, fn):
    """Apply fn to the session state under the lock and save it; returns fn's result."""
    with _lock:
        st = _load(sid)
        result = fn(st)
        _save(sid, st)
        return result


def log(sid, msg, eid=None):
    """Append a line to the session's activity log (last 500 kept)."""
    def f(st):
        st["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg, "episode": eid})
        del st["log"][:-500]
    update(sid, f)


def next_id(items, prefix):
    """Next free id with a prefix, e.g. d3 or e2."""
    nums = [int(x["id"][len(prefix):]) for x in items if x["id"].startswith(prefix) and x["id"][len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


def find(items, item_id):
    """The item with this id in a list, or None."""
    return next((x for x in items if x["id"] == item_id), None)


def list_sessions():
    """Summary of every session, newest first."""
    out = []
    for d in config.SESSIONS_DIR.iterdir():
        if d.is_dir() and valid_id(d.name) and (d / "state.json").exists():
            try:
                st = get(d.name)
            except Exception:
                continue
            out.append({"id": st["id"], "title": st["title"], "updated": st.get("updated", st["created"]),
                        "documents": len(st["documents"]), "episodes": len(st["episodes"])})
    return sorted(out, key=lambda x: x["updated"], reverse=True)


def delete(sid):
    """Delete a session folder."""
    with _lock:
        _cache.pop(sid, None)
        for k in [k for k in _blocks_cache if k[0] == sid]:
            _blocks_cache.pop(k, None)
        shutil.rmtree(session_dir(sid), ignore_errors=True)


# --- document blocks (written once by documents.analyze, read many times) ---

def blocks_path(sid, did):
    """Path of a document's blocks.json."""
    return session_dir(sid) / "docs" / did / "blocks.json"


def save_blocks(sid, did, blocks):
    """Save a document's text blocks."""
    p = blocks_path(sid, did)
    p.write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    with _lock:
        _blocks_cache[(sid, did)] = blocks


def load_blocks(sid, did):
    """A document's text blocks (cached)."""
    with _lock:
        if (sid, did) not in _blocks_cache:
            p = blocks_path(sid, did)
            _blocks_cache[(sid, did)] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        return _blocks_cache[(sid, did)]
