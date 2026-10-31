"""Loads agent settings (config/agents.yaml) and prompts (prompts/*.md).

Both files are re-read when they change on disk, so you can tune a prompt or swap a model
without restarting the server. A model can also be overridden from .env: MODEL_<AGENT_NAME>.
"""
import os
import re
import threading
from dataclasses import dataclass

import yaml

from .. import config

_lock = threading.Lock()
_cache = {}  # path -> (mtime, content)


def _read(path, parse=False):
    with _lock:
        mtime = path.stat().st_mtime
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        text = path.read_text(encoding="utf-8")
        val = yaml.safe_load(text) if parse else text
        _cache[path] = (mtime, val)
        return val


@dataclass
class AgentSpec:
    name: str
    model: str
    temperature: float
    prompt: str
    vision: bool = False
    max_tokens: int = None
    variants: dict = None


def spec(name):
    """The AgentSpec (model, temperature, prompt file, vision) for one agent, from config/agents.yaml."""
    all_specs = _read(config.CONFIG_DIR / "agents.yaml", parse=True) or {}
    if name not in all_specs:
        raise KeyError(f"No agent called '{name}' in config/agents.yaml")
    s = all_specs[name]
    variants = {k[len("prompt_"):]: v for k, v in s.items() if k.startswith("prompt_")}
    return AgentSpec(
        name=name,
        model=os.getenv(f"MODEL_{name.upper()}", "").strip() or s["model"],
        temperature=float(s.get("temperature", 0.5)),
        prompt=s["prompt"],
        vision=bool(s.get("vision", False)),
        max_tokens=s.get("max_tokens"),
        variants=variants,
    )


_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(text, **values):
    """Fill {{name}} placeholders. Unknown placeholders are left as they are."""
    return _VAR.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def system_prompt(name, variant=None, **values):
    """The agent's system prompt (prompts/<file>.md, or a variant) with its {{placeholders}} filled in."""
    s = spec(name)
    file = (s.variants or {}).get(variant) if variant else s.prompt
    if not file:
        raise KeyError(f"Agent '{name}' has no prompt variant '{variant}'")
    return render(_read(config.PROMPTS_DIR / file), **values)


def all_models():
    """{agent name: model id} for every configured agent (shown in /api/health)."""
    return {k: spec(k).model for k in (_read(config.CONFIG_DIR / "agents.yaml", parse=True) or {})}
