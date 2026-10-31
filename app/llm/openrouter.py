"""Thin OpenRouter client (OpenAI-compatible chat completions), with image support."""
import base64
import json
import re
import time

import requests

from .. import config


class LLMError(Exception):
    pass


_FATAL = (401, 402, 403)  # bad key / no credits / blocked: retrying will not help


def image_part(png_bytes):
    """A chat-message content part carrying a PNG image as a data URI."""
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}}


def chat(model, messages, *, json_mode=False, temperature=0.7, max_tokens=None, retries=3):
    """One chat-completion call to OpenRouter; returns the reply text. Retries on rate limits and server errors."""
    if not config.OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY is not set (add it to .env)")
    body = {"model": model, "messages": messages, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens:
        body["max_tokens"] = max_tokens
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(config.OPENROUTER_URL, headers=headers, json=body, timeout=180)
            if r.status_code in _FATAL:
                raise LLMError(f"OpenRouter rejected the request (HTTP {r.status_code}): {r.text[:200]}")
            if r.status_code == 404:
                raise LLMError(f"OpenRouter does not know the model '{model}'. Check config/agents.yaml. {r.text[:150]}")
            if r.status_code == 400 and "response_format" in body:
                body.pop("response_format")  # some models do not support JSON mode; fall back to prompt-only JSON
                last = LLMError(f"HTTP 400: {r.text[:200]}")
                continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if not content:
                raise LLMError("empty completion")
            return content
        except LLMError as e:
            if "rejected the request" in str(e) or "does not know the model" in str(e):
                raise
            last = e
        except (requests.RequestException, KeyError, ValueError, IndexError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"OpenRouter call failed after {retries} attempts: {last}")


def extract_json(text):
    """Pull a JSON object out of a model reply, tolerating code fences and chatter."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found in model reply")
