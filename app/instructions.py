"""What the student types for the AI (the request box, "Ask AI to revise", the custom level of detail) can be in
any language. The agents work in English, so text in another script is translated to English with Bodhan first.

Design:
- English or romanized text (e.g. Hinglish "photosynthesis samjhao") is passed through: the LLMs read it fine,
  and translating it would only risk changing it. So is mostly-English text with a few Indian-script words
  ("about रंध्र"): those words are usually a topic named the way the PDF names it, and must stay exact.
- Text mostly in an Indian script is translated to English. The ORIGINAL is kept next to the translation, because
  a topic named in Hindi may have to be matched against a Hindi PDF, and names/terms can suffer in translation.
- Numbers, page references and PDF names are kept by the translator; if the translation loses the digits of the
  original, or fails, the original text is used alone (the agents may still understand it) and the log says so.
- The student always sees their own words on screen; only the agents get the English version.
"""
import re

from . import config, options, translation
from .text import detect_language

_DIGITS = re.compile(r"\d+")
_INDIC_DIGITS = str.maketrans("०१२३४५६७८९০১২৩৪৫৬৭৮৯੦੧੨੩੪੫੬੭੮੯૦૧૨૩૪૫૬૭૮૯୦୧୨୩୪୫୬୭୮୯௦௧௨௩௪௫௬௭௮௯౦౧౨౩౪౫౬౭౮౯೦೧೨೩೪೫೬೭೮೯൦൧൨൩൪൫൬൭൮൯",
                              "0123456789" * 9)


def needs_translation(text):
    """True when the text is mostly in a non-Latin (Indian) script."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return False
    non_latin = sum(1 for c in letters if ord(c) > 0x024F)
    return non_latin / len(letters) > 0.5


def for_agents(text, emit=None, what="the request"):
    """English text for the agents (with the original kept), or the text unchanged."""
    text = (text or "").strip()
    if not text or not needs_translation(text):
        return text
    lang = detect_language("".join(c for c in text if ord(c) > 0x024F))  # name the Indian script, not the English words
    name = options.language_name(lang) if lang in options.table()["languages"] else "another language"
    if not config.BODHAN_TRANSLATE_API_KEY:
        if emit:
            emit(f"Translator: {what} is in {name}, but Bodhan translation is not set up; the agents get it as written")
        return text
    try:
        english = translation.translate_text(text, "en").strip()
    except Exception as e:
        if emit:
            emit(f"Translator: could not translate {what} from {name} ({e}); the agents get it as written")
        return text
    want = sorted(_DIGITS.findall(text.translate(_INDIC_DIGITS)))
    if want and sorted(_DIGITS.findall(english)) != want:  # page numbers etc. must survive
        if emit:
            emit(f"Translator: the English version of {what} changed its numbers; the agents get the original")
        return text
    if emit:
        emit(f"Translator: {what} was in {name}; translated to English for the agents")
    return f"{english}\n(The student's original words, in {name}: {text})"
