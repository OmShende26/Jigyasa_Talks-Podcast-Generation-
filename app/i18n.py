"""Interface language: the UI's English strings (static/i18n/en.json + option labels) and their saved
translations (static/i18n/<code>.json, written once by `python -m app.build_ui_text`). Serving is instant:
no API call happens when the user switches language. A string missing from a translation shows in English.
"""
import json

from . import config, options

EN_FILE = config.STATIC_DIR / "i18n" / "en.json"


def english():
    """All interface strings in English: static/i18n/en.json plus the option labels from options.yaml."""
    strings = json.loads(EN_FILE.read_text(encoding="utf-8"))
    t = options.table()
    for group in ("ages", "formats", "subjects", "lengths", "depths"):
        for k, v in t[group].items():
            strings[f"opt.{group}.{k}.label"] = v["label"]
            if v.get("blurb"):
                strings[f"opt.{group}.{k}.blurb"] = v["blurb"]
    return strings


def ui_file(lang):
    """Path of the saved interface translation for a language."""
    return config.STATIC_DIR / "i18n" / f"{lang}.json"


def available():
    """[{"id", "label", "ready"}] for every interface language; ready = its translation has been saved."""
    names = options.table().get("ui_languages", {"en": "English"})
    return [{"id": k, "label": v, "ready": k == "en" or ui_file(k).exists()} for k, v in names.items()]


def _named(strings, lang):
    """The app name is fixed per language in config/options.yaml (app_names), not left to the translator."""
    names = options.table().get("app_names", {})
    if names.get(lang) or names.get("en"):
        strings["app.name"] = names.get(lang) or names["en"]
    return strings


def strings(lang):
    """Interface strings for a language (saved translation merged over English), with the app name in that script."""
    en = english()
    if lang == "en" or not ui_file(lang).exists():
        return {"status": "ready", "strings": _named(en, "en"), "done": len(en), "total": len(en)}
    saved = json.loads(ui_file(lang).read_text(encoding="utf-8"))
    tr, src = saved.get("strings", {}), saved.get("source", {})
    merged = {k: (tr[k] if k in tr and src.get(k) == v else v) for k, v in en.items()}
    done = sum(1 for k, v in en.items() if k in tr and src.get(k) == v)
    return {"status": "ready", "strings": _named(merged, lang), "done": done, "total": len(en)}
