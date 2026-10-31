"""Write the interface text in other languages, once:   python -m app.build_ui_text [hi mr ...]

Uses the "ui_translator" agent (config/agents.yaml, Gemini by default; prompt in prompts/ui_translator.md) with
your OPENROUTER_API_KEY. The result is saved as static/i18n/<code>.json, so switching the interface language is
instant and needs no API call afterwards. Run it again after changing static/i18n/en.json or the option labels:
only new or changed strings are translated. To fix a wording by hand, edit the saved file (it is plain JSON).
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from . import config, i18n, options
from .agents import base

_PH = re.compile(r"\{\w+\}")
BATCH = 40


def _batch(lang_name, items):
    data = base.run_json("ui_translator", json.dumps(dict(items), ensure_ascii=False, indent=1), language=lang_name)
    out = {}
    for k, en in items:
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, str) and v.strip() and sorted(_PH.findall(v)) == sorted(_PH.findall(en)):
            out[k] = v.strip()
    return out


def build(lang):
    """Translate the missing or changed interface strings for one language and save static/i18n/<lang>.json. Returns the number left in English."""
    names = options.table().get("ui_languages", {})
    lang_name = options.language_name(lang)
    en = {k: v for k, v in i18n.english().items() if k != "app.name"}  # the name comes from app_names
    path = i18n.ui_file(lang)
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"strings": {}, "source": {}}
    todo = [(k, v) for k, v in en.items() if saved["source"].get(k) != v or k not in saved["strings"]]
    if not todo:
        print(f"{lang} ({names.get(lang, lang_name)}): up to date")
        return 0
    print(f"{lang} ({names.get(lang, lang_name)}): translating {len(todo)} strings…", flush=True)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    missing = 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        for items, res in zip(batches, pool.map(lambda b: _safe(lang_name, b), batches)):
            for k, v in items:
                if k in res:
                    saved["strings"][k], saved["source"][k] = res[k], v
                else:
                    missing += 1
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"  saved {path.name}" + (f" ({missing} strings stay in English; run again to retry them)" if missing else ""))
    return missing


def _safe(lang_name, items):
    for _ in range(2):
        try:
            return _batch(lang_name, items)
        except Exception as e:
            err = e
    print(f"  a batch failed: {err}")
    return {}


def main():
    """Command-line entry point: build every interface language (or the ones given as arguments)."""
    if not config.OPENROUTER_API_KEY:
        sys.exit("OPENROUTER_API_KEY is not set in .env")
    langs = sys.argv[1:] or [k for k in options.table().get("ui_languages", {}) if k != "en"]
    for lang in langs:
        build(lang)
    print("Done. Restart is not needed: reload the page and pick the language at the top right.")


if __name__ == "__main__":
    main()
