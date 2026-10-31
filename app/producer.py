"""Produces one segment.

AI path (first draft or "Ask AI to revise"):
    Script Writer <-> (Audience Editor -> Reviewer) loop, Speech Director, [Bodhan translation], TTS, timeline.
    For languages written via English, the agents work on the English script; only approved lines are translated.
Manual path ("Edit the script myself"):
    the user's edited lines go straight to TTS. No agents, no translation.

Every turn in the result carries:
    text      what is shown on screen      speech    what was sent to TTS
    original  the checked script line the translation was made from (translated languages only)
    delivery  Speech Director's preset     refs      source block ids
    doc/page  the PDF page to show while it plays
    t0/t1     start/end time (seconds) inside the segment audio
"""
import re
import time
from pathlib import Path

from . import config, options, speech_rates, translation
from .agents import audience_editor, final_checker, host_framer, reviewer, script_writer, speech_director
from .speech import audio, bodhan
from .text import mismatch, speech_safe, split_for_tts

_asr_off = False  # set once if Bodhan says this key cannot use speech-to-text


def asr_active():
    """True while the speech-recognition check of recorded lines is enabled and allowed by the key."""
    return config.QA_ENABLED and not _asr_off


def turn_gap_ms(j):
    """Pause before turn j: varies inside PAUSE_TURN_MIN_MS..PAUSE_TURN_MAX_MS so it does not sound mechanical."""
    if j == 0:
        return 0
    lo, hi = config.PAUSE_TURN_MIN_MS, max(config.PAUSE_TURN_MIN_MS, config.PAUSE_TURN_MAX_MS)
    steps = (0.35, 0.8, 0.1, 0.6, 1.0, 0.25)  # fixed pattern: same script -> same timing
    return int(lo + (hi - lo) * steps[j % len(steps)])


def _attach_pages(turns, seg, blocks):
    """Each turn gets the page of its first source reference; otherwise it keeps the previous turn's page."""
    first = blocks.get(seg["block_ids"][0]) if seg["block_ids"] else None
    cur = (first["doc"], first["page"]) if first else (None, None)
    for t in turns:
        ref = next((blocks[r] for r in t.get("refs", []) if r in blocks), None)
        if ref:
            cur = (ref["doc"], ref["page"])
        t["doc"], t["page"] = cur


def _render(turns, work: Path, out_mp3: Path, emit, label, lang):
    """TTS every sentence clip, check with ASR if available, stitch with exact pauses, record start/end times."""
    global _asr_off
    work.mkdir(parents=True, exist_ok=True)
    items, qa, t = [], [], 0.0
    try:
        for j, turn in enumerate(turns):
            voice = config.HOST_A_VOICE if turn["speaker"] == "A" else config.HOST_B_VOICE
            style = (turn["style"] if "style" in turn else options.bodhan_style(turn.get("delivery"))) or None
            worst, heard_worst, retries, turn_start = None, None, 0, None
            for k, chunk in enumerate(split_for_tts(turn["speech"])):
                best, best_mm, best_heard = None, None, None
                for attempt in range(config.TTS_RETRIES + 1):
                    data = bodhan.tts(chunk, voice, style, lang)
                    mm, heard = None, None
                    if asr_active():
                        try:
                            heard = bodhan.asr(data, lang)
                            mm = mismatch(chunk, heard)
                        except Exception as e:
                            if "HTTP 40" in str(e):
                                _asr_off = True
                                emit("Audio check is off: this Bodhan key cannot use speech-to-text.")
                    if best is None or (mm is not None and (best_mm is None or mm < best_mm)):
                        best, best_mm, best_heard = data, mm, heard
                    if mm is None or mm <= config.MISMATCH_THRESHOLD:
                        break
                    retries += 1
                    emit(f"Audio check · {label}: line {j + 1} sounded off ({mm:.0%} different), recording again")
                raw, wav = work / f"t{j:03d}_{k:02d}.raw", work / f"t{j:03d}_{k:02d}.wav"
                raw.write_bytes(best)
                audio.to_norm_wav(raw, wav)
                raw.unlink(missing_ok=True)
                gap = turn_gap_ms(j) if k == 0 else config.PAUSE_SENTENCE_MS
                items.append((wav, gap))
                t += gap / 1000.0
                if turn_start is None:
                    turn_start = t
                t += audio.wav_seconds(wav)
                if best_mm is not None and (worst is None or best_mm > worst):
                    worst, heard_worst = best_mm, best_heard
            turn["t0"], turn["t1"] = round(turn_start, 3), round(t, 3)
            rec = {"turn": j + 1, "mismatch": worst, "retries": retries}
            if worst is not None and worst > config.MISMATCH_THRESHOLD:
                rec["heard"] = heard_worst
            qa.append(rec)
        audio.stitch(items, out_mp3)
    finally:
        for p, _ in items:
            Path(p).unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass
    return qa, round(t, 3)


def _record(ep_dir, seg, version, turns, emit, label, lang):
    emit(f"Audio · {label}: recording {len(turns)} lines")
    rel = f"audio/seg{seg['idx']}_v{version}.mp3"
    qa, total = _render(turns, ep_dir / "audio" / f".seg{seg['idx']}_v{version}", ep_dir / rel, emit, label, lang)
    return {"qa": qa, "qa_flagged_turns": [q["turn"] for q in qa if q.get("heard") is not None],
            "audio": rel, "duration": audio.duration(ep_dir / rel) or total}


def _words(turns):
    return sum(len(t["text"].split()) for t in turns)


_LONGER = re.compile(r"\b(longer|more detail|in detail|elaborate|expand|add (an? )?(real-life )?examples?|more examples?|explain .*more|slowly)\b", re.I)
_SHORTER = re.compile(r"\b(shorter|short|brief|concise|summari[sz]e|cut|less)\b", re.I)


def _target_for(seg, note, previous_turns):
    """Word budget for this version. A change request that asks for more (an example, more detail) or less
    (shorter) moves the budget, so the length step does not undo what the student asked for."""
    target = seg.get("target_words") or 250
    if not note:
        return target
    base = max(target, _words(previous_turns or [])) if _LONGER.search(note) else target
    if _SHORTER.search(note) and not _LONGER.search(note):
        return max(40, int(min(target, _words(previous_turns or []) or target) * 0.7))
    if _LONGER.search(note):
        return int(base * 1.3)
    return target


def _drop_unconfirmed(turns, unresolved, meta, emit, label):
    """After the last review round, lines the Reviewer still finds unsupported by the source are removed
    (better a shorter segment than a wrong fact). Kept if removing them would leave too little."""
    bad = sorted({int(i["turn"]) for i in unresolved if i.get("type") in reviewer.BAD
                  and str(i.get("turn") or "").isdigit() and 1 <= int(i["turn"]) <= len(turns)})
    if not bad:
        return turns, []
    keep = [t for n, t in enumerate(turns, 1) if n not in bad]
    if len(keep) < max(2, len(turns) // 2):
        return turns, []
    emit(f"Reviewer · {label}: removed {len(bad)} line(s) that could not be confirmed from the source")
    return keep, [turns[n - 1]["text"] for n in bad]


def _fit_length(wmeta, seg, turns, verdict, emit, label, note=None):
    """Keep the segment inside its word budget: if the approved script is more than LENGTH_TOLERANCE over,
    the writer shortens it (up to twice, each time checked by the Reviewer; kept only if it stays accurate)."""
    target = seg.get("target_words") or 250
    for attempt in range(2):
        n = _words(turns)
        if n <= target * (1 + config.LENGTH_TOLERANCE):
            return turns
        aim = int(target * (1.0 if attempt == 0 else 0.85))
        emit(f"Length check · {label}: {n} words for a budget of {target}; shortening")
        try:
            short = script_writer.write(wmeta, seg, draft=turns, note=(
                f"TOO LONG: the script has {n} words but must fit in {aim} words so the podcast keeps the length "
                f"the student chose. Rewrite it in at most {aim} words: keep the most important ideas, drop "
                "repetition and side remarks, keep the same speakers and style."
                + (f" Keep this change the student asked for: {note}" if note else "")))
            if _words(short) >= n:
                return turns
            v2 = reviewer.review(wmeta, seg, short, note=note)
            if any(i.get("type") in reviewer.BAD for i in v2["issues"]):
                emit(f"Length check · {label}: the shorter version lost accuracy; keeping the approved one")
                return turns
            if note and verdict.get("request") and v2.get("request"):
                verdict["request"] = v2["request"]
            emit(f"Length check · {label}: shortened to {_words(short)} words")
            turns = short
        except Exception as e:
            emit(f"Length check · {label}: could not shorten ({e})")
            return turns
    return turns


def produce(ep_dir: Path, meta, seg, version, emit, note=None, previous_turns=None):
    """AI path: write, adapt to the age group, review, direct, (translate), record."""
    label = f"segment {seg['idx'] + 1}"
    target = meta["settings"].get("language", "en")
    write_lang = options.write_language(target)  # the agents write and check in this language
    wmeta = {**meta, "settings": {**meta["settings"], "language": write_lang}}
    old_frames = {"opening": [], "closing": []}
    if previous_turns:  # a translated or hand-edited earlier version: revise the checked original
        previous_turns = [{**t, "text": t.get("original") or t["text"]} for t in previous_turns]
        for t in previous_turns:  # the welcome/goodbye are kept as they were; only the teaching is revised
            if t.get("frame") in old_frames:
                old_frames[t["frame"]].append({k: v for k, v in t.items() if k in ("speaker", "text", "refs", "frame")})
        previous_turns = [t for t in previous_turns if not t.get("frame")]
    draft, issues, rounds, turns, verdict = (previous_turns if note else None), None, [], None, None
    seg = {**seg, "target_words": _target_for(seg, note, previous_turns)}  # "shorter" / "add an example" change the budget
    for attempt in range(config.MAX_REVISIONS + 1):
        emit(f"Script Writer · {label}: {'revising' if attempt else 'drafting'} (round {attempt + 1})")
        turns = script_writer.write(wmeta, seg, draft=draft, issues=issues, note=note)
        turns = audience_editor.adapt(wmeta, turns, emit, label)
        emit(f"Reviewer · {label}: checking {'the facts' if seg['kind'] == 'foundations' else 'against the source'}")
        verdict = reviewer.review(wmeta, seg, turns, note=note)
        rounds.append({"round": attempt + 1, "passed": verdict["passed"], "issues": verdict["issues"]})
        if verdict["passed"]:
            emit(f"Reviewer · {label}: approved")
            break
        emit(f"Reviewer · {label}: {len(verdict['issues'])} issue(s), sending back to the writer")
        draft, issues = turns, verdict["issues"]
    unresolved = [] if verdict["passed"] else verdict["issues"]
    turns, removed = _drop_unconfirmed(turns, unresolved, meta, emit, label)
    if removed:
        unresolved = [i for i in unresolved if i.get("type") not in reviewer.BAD]
    if not note:  # "Revise with AI": the student's request decides the length, so no length step
        turns = _fit_length(wmeta, seg, turns, verdict, emit, label)
    request_check = verdict.get("request") if note else None
    if request_check:
        emit(f"Reviewer · {label}: your change was {'made' if request_check['done'] == 'yes' else 'partly made' if request_check['done'] == 'partly' else 'NOT made'}"
             + (f" ({request_check['note']})" if request_check.get("note") else ""))
    # welcome before the first segment, goodbye after the last (Host Framer, or kept from the previous version)
    if seg["idx"] == 0:
        turns = (old_frames["opening"] or host_framer.frame(wmeta, "opening", emit, label)) + turns
    if seg["idx"] == meta["total"] - 1:
        turns = turns + (old_frames["closing"] or host_framer.frame(wmeta, "closing", emit, label))
    if unresolved:
        emit(f"Reviewer · {label}: still {len(unresolved)} issue(s) after {len(rounds)} rounds; flagged for your review")

    emit(f"Speech Director · {label}: choosing delivery")
    speech_director.direct(turns, emit, label, write_lang)
    _attach_pages(turns, seg, meta["blocks"])
    if target != write_lang:
        translation.translate_turns(turns, target, emit, label)
    final_issues = _final_check(turns, write_lang, target, emit, label)
    for t_ in turns:  # the Bodhan voice style for each line, adjusted for the class level and format
        t_["style"] = options.bodhan_style(t_.get("delivery"), meta["settings"]) or ""

    rec = _record(ep_dir, seg, version, turns, emit, label, target)
    script_words = sum(len((t_.get("original") or t_["text"]).split()) for t_ in turns)
    speech_rates.record(target, rec["duration"], script_words)  # makes the next podcast's length more exact
    return {
        "v": version, "kind": "ai" if note or previous_turns else "initial",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note, "turns": turns,
        "rounds": rounds, "passed": bool(verdict["passed"]), "unresolved": unresolved,
        "written_in": write_lang, "language": target, "final_issues": final_issues,
        "request_check": request_check, "removed_lines": removed, **rec,
    }


def _final_check(turns, write_lang, target, emit, label):
    """Final Checker before TTS: fix what can be fixed, return the issues that remain (shown to the user)."""
    if not config.FINAL_CHECK or not final_checker.pairs(turns):
        return []
    emit(f"Final Checker · {label}: checking numbers and facts in the lines to be spoken")
    try:
        found = final_checker.check(turns, write_lang, target)
    except Exception as e:
        emit(f"Final Checker · {label}: skipped ({e})")
        return []
    if not found:
        emit(f"Final Checker · {label}: everything matches")
        return []
    if target == write_lang:  # only the speech form differs: speak the approved wording instead
        for n in found:
            t = turns[n - 1]
            t["speech"] = speech_safe(t["text"], target)
        emit(f"Final Checker · {label}: {len(found)} line(s) differed from the approved text; using the approved wording")
        return []
    emit(f"Final Checker · {label}: {len(found)} line(s) changed in translation ({'; '.join(x['detail'] for x in found.values())[:160]}); translating again")
    for n in found:
        try:
            translation.retranslate_turn(turns[n - 1], target)
        except Exception as e:
            emit(f"Final Checker · {label}: line {n} could not be translated again ({e})")
    try:
        left = final_checker.check(turns, write_lang, target, only=set(found))
    except Exception:
        left = found
    if left:
        emit(f"Final Checker · {label}: {len(left)} line(s) still differ; flagged for you to check")
    else:
        emit(f"Final Checker · {label}: fixed")
    return list(left.values())


def rerecord(ep_dir: Path, meta, seg, version, emit, base_version, lines):
    """Manual path: the user's edited lines (in the podcast language) -> TTS only. No agents, no translation.

    lines: list of new texts, one per turn of base_version (same order). Unchanged lines keep their speech text.
    """
    label = f"segment {seg['idx'] + 1}"
    lang = meta["settings"].get("language", "en")
    old = base_version["turns"]
    if len(lines) != len(old):
        raise ValueError("The edited script must keep the same lines")
    turns, changed = [], 0
    for t, new in zip(old, lines):
        new = " ".join(str(new or "").split())
        if not new:
            raise ValueError("A line cannot be empty")
        nt = {k: v for k, v in t.items() if k not in ("t0", "t1")}
        if new != t["text"]:
            changed += 1
            nt["text"], nt["speech"], nt["edited"] = new, speech_safe(new, lang), True
            nt.pop("original", None)  # the English original no longer matches this line
        turns.append(nt)
    emit(f"Manual edit · {label}: {changed} line(s) changed; re-recording the audio only")
    rec = _record(ep_dir, seg, version, turns, emit, label, lang)
    return {
        "v": version, "kind": "manual", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": f"Edited by hand ({changed} line{'s' if changed != 1 else ''})", "turns": turns,
        "rounds": base_version.get("rounds", []), "passed": base_version.get("passed", True) and changed == 0,
        "unresolved": base_version.get("unresolved", []), "edited_lines": changed,
        "written_in": base_version.get("written_in", lang), "language": lang, **rec,
    }
