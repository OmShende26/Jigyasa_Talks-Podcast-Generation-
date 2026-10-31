"""Bodhan AI client: text-to-speech (and optional speech-to-text for the audio check). No OCR."""
import json
import threading
import time

import requests

from .. import config


class BodhanError(Exception):
    pass


def _headers(key_name="BODHAN_API_KEY"):
    key = getattr(config, key_name)
    if not key:
        raise BodhanError(f"{key_name} is not set (add it to .env)")
    return {"Authorization": f"Bearer {key}"}


# Calls to one endpoint are spaced out so parallel segments do not trip Bodhan's rate limit.
_gate = threading.Lock()
_next_ok = {}


def _throttle(path):
    gap = config.BODHAN_MIN_INTERVAL
    if gap <= 0:
        return
    with _gate:
        now = time.monotonic()
        start = max(now, _next_ok.get(path, 0.0))
        _next_ok[path] = start + gap
    if start > now:
        time.sleep(start - now)


def _post(path, retries=6, key_name="BODHAN_API_KEY", **kw):
    last = None
    headers = _headers(key_name)  # a missing key fails at once, without retries
    for attempt in range(retries):
        _throttle(path)
        try:
            url = path if path.startswith("http") else f"{config.BODHAN_BASE}{path}"
            r = requests.post(url, headers=headers, timeout=180, **kw)
            if r.status_code in (401, 402, 403):
                raise BodhanError(f"Bodhan rejected the request (HTTP {r.status_code}): {r.text[:200]}")
            if r.status_code == 429 or r.status_code >= 500:
                last = BodhanError(f"HTTP {r.status_code}: {r.text[:200]}")
                try:
                    delay = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    delay = min(30, 3 * 2**attempt)
                time.sleep(min(delay, 60))
                continue
            if 400 <= r.status_code < 500:  # bad request: retrying will not help; keep Bodhan's error code
                raise BodhanError(f"Bodhan rejected the request (HTTP {r.status_code}): {r.text[:300]}")
            r.raise_for_status()
            return r
        except BodhanError as e:
            if "rejected the request" in str(e):
                raise
            last = e
        except requests.RequestException as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise BodhanError(f"Bodhan {path} failed after {retries} attempts: {last}")


def tts(text, voice, style=None, lang=None):
    """Text -> WAV bytes. `style` is a Bodhan delivery style name, or None for the default."""
    instructions = {"lang": lang or config.LANG}
    if style:
        instructions["style"] = style
    r = _post(
        "/audio/speech",
        json={"model": config.TTS_MODEL, "input": text, "voice": voice, "instructions": json.dumps(instructions)},
    )
    if "json" in r.headers.get("content-type", "") or len(r.content) < 500:
        raise BodhanError(f"TTS returned no audio: {r.content[:200]!r}")
    return r.content


def asr(wav_bytes, lang=None):
    """WAV bytes -> transcript text."""
    r = _post(
        "/audio/transcriptions",
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        data={"model": config.ASR_MODEL, "language": lang or config.LANG, "vad": True},
    )
    return r.json()["text"]


def translate(text, target, max_output_tokens=None):
    """Text -> (translation in `target`, in its native script; truncated flag). Bodhan infers the source language.

    Uses Bodhan's chat-completions transport (model indic-translate): one user turn holding the text, plus
    target_language; the gateway writes the prompt. If BODHAN_TRANSLATE_URL is set in .env, the plain REST
    /translate transport at that address is used instead.
    """
    body = {"text": text, "target_language": target}
    if max_output_tokens:
        body["max_output_tokens"] = max_output_tokens
    if config.BODHAN_TRANSLATE_URL:
        r = _post(config.BODHAN_TRANSLATE_URL, key_name="BODHAN_TRANSLATE_API_KEY", json=body)
        data = r.json()
        if "error" in data:
            raise BodhanError(f"translation failed: {data['error'].get('code')} {data['error'].get('message', '')[:150]}")
        return data["translation"], bool(data.get("truncated"))

    chat = {"model": config.BODHAN_TRANSLATE_MODEL, "target_language": target,
            "messages": [{"role": "user", "content": text}]}
    if max_output_tokens:
        chat["max_output_tokens"] = max_output_tokens
    r = _post("/chat/completions", key_name="BODHAN_TRANSLATE_API_KEY", json=chat)
    data = r.json()
    if "error" in data:
        raise BodhanError(f"translation failed: {data['error'].get('code')} {data['error'].get('message', '')[:150]}")
    choice = data["choices"][0]
    out = (choice.get("message") or {}).get("content") or ""
    truncated = bool(data.get("truncated")) or choice.get("finish_reason") == "length"
    return out.strip(), truncated


def ocr(png_bytes):
    """Page image (PNG) -> text, with Bodhan's indic-ocr model (chat-completions transport)."""
    import base64
    body = {"model": config.OCR_MODEL, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}}]}]}
    r = _post("/chat/completions", key_name="BODHAN_OCR_API_KEY", json=body)
    data = r.json()
    if "error" in data:
        raise BodhanError(f"OCR failed: {data['error'].get('code')} {str(data['error'].get('message', ''))[:150]}")
    return ((data["choices"][0].get("message") or {}).get("content") or "").strip()
