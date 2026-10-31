"""End-to-end test against a running mock server.

    DATA_DIR=/tmp/x MOCK_DELAY=0.2 python tests/run_mock_server.py &
    python tests/e2e.py http://127.0.0.1:8765 [ids_out.json] [DATA_DIR]

With ids_out.json the sessions are kept (ids written there) instead of deleted.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import pymupdf
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
KEEP = Path(sys.argv[2]) if len(sys.argv) > 2 else None
import os as _os
DATA_DIR_ENV = _os.environ.get("DATA_DIR")
FAILS = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def pdf_a(path):
    """6 text pages (figure on page 3) + a scanned page 7."""
    topics = ["Photosynthesis happens in the leaves of green plants", "Roots absorb water and minerals from the soil",
              "Leaves are thin and flat", "Chlorophyll gives leaves their green colour"]
    doc, n = pymupdf.open(), 0
    for pg in range(1, 7):
        page = doc.new_page()
        for i in range(5):
            n += 1
            page.insert_textbox(pymupdf.Rect(60, 60 + i * 105, 540, 155 + i * 105),
                                f"{topics[n % 4]}. Paragraph {n} adds that this process is essential for life. Students can observe it in a simple "
                                "experiment. Teachers often explain that every living thing depends on it, and that careful observation helps us "
                                "notice how the parts of the system work together over time.", fontsize=11)
        if pg == 3:
            for k in range(35):
                page.draw_line((60 + k * 12, 640), (60 + k * 12, 700))
            page.insert_text((60, 730), "Fig. 3.1 The water cycle", fontsize=10)
        page.insert_text((60, 800), "Rationalised 2023-24", fontsize=8)
    tmp = pymupdf.open()
    tp = tmp.new_page()
    tp.insert_textbox(pymupdf.Rect(60, 60, 540, 300), "Scanned page content that only exists as pixels.", fontsize=14)
    doc.new_page().insert_image(doc[-1].rect, stream=tp.get_pixmap(dpi=100).tobytes("png"))
    doc.save(path)


def pdf_b(path):
    """3 pages; only pages 2-3 mention guard cells."""
    doc = pymupdf.open()
    texts = ["Plants exchange gases with the air around them. This exchange keeps them alive.",
             "Guard cells surround each pore on the leaf. Guard cells swell when they take in water and the pore opens.",
             "When guard cells lose water they shrink and the pore closes. This saves water on hot days."]
    for t in texts:
        page = doc.new_page()
        for i in range(4):
            page.insert_textbox(pymupdf.Rect(60, 60 + i * 120, 540, 170 + i * 120), f"{t} Paragraph {i + 1} explains this with care and detail for the reader to follow along slowly.", fontsize=11)
    doc.save(path)


HINDI = ["प्रकाश संश्लेषण हरे पौधों की पत्तियों में होता है। पौधे सूर्य के प्रकाश से अपना भोजन बनाते हैं।",
         "जड़ें मिट्टी से पानी और खनिज लवण सोखती हैं। यह पानी तने के रास्ते पत्तियों तक पहुँचता है।",
         "पत्तियों में छोटे छिद्र होते हैं जिन्हें रंध्र कहते हैं। इनसे कार्बन डाइऑक्साइड अंदर जाती है।"]


def pdf_hi(path):
    """3 pages of Hindi text (needs a Devanagari font: FreeSans)."""
    font = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
    doc = pymupdf.open()
    for t in HINDI:
        page = doc.new_page()
        for i in range(4):
            page.insert_textbox(pymupdf.Rect(60, 60 + i * 120, 540, 170 + i * 120), f"{t} अनुच्छेद {i + 1} में इसे विस्तार से समझाया गया है ताकि विद्यार्थी ध्यान से पढ़ सकें।",
                                fontsize=12, fontfile=font, fontname="FreeSans")
    doc.save(path)


def tts_langs(data_dir):
    p = Path(data_dir) / "tts_calls.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def cur(seg):
    return next(v for v in seg["versions"] if v["v"] == seg["current_v"])


def tam(text):
    return sum(1 for c in text if "\u0b80" <= c <= "\u0bff") / max(1, sum(c.isalpha() for c in text))


def setting_line(turns, key="text"):
    """The fake writer's "Setting check: ..." line (segment 1 starts with the speakers' hello)."""
    return next(t for t in turns if "Setting check" in (t.get("original") or t["text"]) or "Setting check" in t[key])


def sline(turns):
    return next(i for i, t in enumerate(turns) if "Setting check" in (t.get("original") or t["text"]))


def j(r):
    try:
        return r.json()
    except ValueError:
        return {}


def sess(sid):
    return requests.get(f"{BASE}/api/sessions/{sid}").json()


def wait(sid, cond, timeout=300, step=0.3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = sess(sid)
        if cond(st):
            return st
        time.sleep(step)
    raise TimeoutError("timed out")


def ep_of(st, eid):
    return next(e for e in st["episodes"] if e["id"] == eid)


def ep_done(eid):
    def f(st):
        e = ep_of(st, eid)
        return e["status"] in ("done", "error", "not_found") and all(s["status"] in ("done", "error") for s in e["segments"])
    return f


def upload(sid, *paths):
    files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in paths]
    return requests.post(f"{BASE}/api/sessions/{sid}/documents", files=files)


def say(sid, mode, text="", settings=None):
    return requests.post(f"{BASE}/api/sessions/{sid}/messages", json={"mode": mode, "text": text, "settings": settings or {}})


def main():
    tmp = Path(tempfile.mkdtemp())
    a, b = tmp / "plants.pdf", tmp / "leaves.pdf"
    pdf_a(a)
    pdf_b(b)

    h = requests.get(f"{BASE}/api/health").json()
    check(h["models"]["document_analyst"] == "google/gemini-2.5-flash" and h["models"]["reviewer"] != h["models"]["script_writer"],
          f"models come from config/agents.yaml; reviewer differs from writer ({h['models']})")
    o = requests.get(f"{BASE}/api/options").json()
    check([x["id"] for x in o["formats"]] == ["friends", "teacher_student", "interview", "solo"], "options come from config/options.yaml")

    # ------------------------------------------------ session 1: two PDFs
    s1 = requests.post(f"{BASE}/api/sessions").json()["id"]
    check(say(s1, "full").status_code == 400, "a request before any PDF is refused")
    check(upload(s1, tmp / "x.pdf" if (tmp / "x.pdf").write_bytes(b"nope") else a).status_code == 400, "a non-PDF upload is refused")
    r = upload(s1, a, b)
    check(r.status_code == 200 and j(r)["ids"] == ["d1", "d2"], "two PDFs uploaded in one go")
    st = wait(s1, lambda s: all(d["status"] != "analyzing" for d in s["documents"]))
    d1, d2 = st["documents"]
    check(d1["status"] == "ready" and d2["status"] == "ready", "both PDFs read")
    check(d1["info"]["scanned_pages"] == [7] and d1["info"]["unread_pages"] == [], "scanned page 7 read by the Document Analyst (no OCR service)")
    check(sorted(d1["info"]["vision_pages"]) == [3, 7] and d1["info"]["visuals"] == 1, f"only pages with scans or visuals went to the vision model ({d1['info']})")
    check(requests.get(f"{BASE}/files/{s1}/docs/d1/pages/p3.png").content[:4] == b"\x89PNG", "page images are served for the viewer")
    check(st["title"] == "plants", f"session titled after the first PDF ({st['title']!r})")

    # ------------------------------------------------ podcast 1: whole
    check(say(s1, "full", settings={"format": "nope"}).status_code == 400, "an unknown style choice is refused")
    check(say(s1, "part", "").status_code == 400, "'a specific part' needs a topic")
    r = say(s1, "full")
    e1 = j(r)["episode"]
    st = wait(s1, ep_done(e1))
    e = ep_of(st, e1)
    check(e["status"] == "done", f"podcast 1 finished ({e['status']}, {e.get('error')})")
    check("previous=0" in e["understanding"] and "mode=full" in e["understanding"], f"interpreter knew this was the first request ({e['understanding']})")
    check(e["scope_text"] == "all uploaded files (d1, d2)", f"whole podcast uses every file ({e['scope_text']})")
    segs = e["segments"]
    check(len(segs) == 4, f"standard length gives 4 segments ({len(segs)})")
    docs_used = {i[:2] for s in segs for i in s["block_ids"]}
    check(docs_used == {"d1", "d2"}, f"blocks from both PDFs are used ({docs_used})")
    v0, v1, v2 = (s["versions"][0] for s in segs[:3])
    check(len(v0["rounds"]) == 2 and v0["passed"] and not any("cheese" in t["text"] for t in v0["turns"]), "seg 1: reviewer caught the unsupported claim; writer fixed it")
    check(any(q["retries"] >= 1 for q in v1["qa"]) and not v1["qa_flagged_turns"], "seg 2: audio check recorded the misheard line again")
    check(len(v2["rounds"]) == 3 and not v2["passed"], "seg 3: never approved, flagged for review")

    all_turns = [(s, t) for s in segs for t in s["versions"][0]["turns"]]
    ok_time = all(0 <= t["t0"] < t["t1"] for _, t in all_turns) and all(
        all(a["t1"] <= b["t0"] + 1e-6 for a, b in zip(s["versions"][0]["turns"], s["versions"][0]["turns"][1:])) for s in segs)
    check(ok_time, "every line has start/end times, in order, without overlap")
    check(all(s["versions"][0]["turns"][-1]["t1"] <= s["versions"][0]["duration"] + 0.3 for s in segs), "line times fit inside the segment audio")
    check(all(t["doc"] and t["page"] for _, t in all_turns), "every line knows which PDF page to show")
    fig_lines = [t for _, t in all_turns if t["text"].startswith("Picture this")]
    check(fig_lines and fig_lines[0]["doc"] == "d1" and fig_lines[0]["page"] == 3, "the figure line points at page 3, where the figure is")
    ref_lines = [t for _, t in all_turns if t["refs"]]
    check(ref_lines and all(t["page"] == int(t["refs"][0].split("p")[1].split("b")[0]) for t in ref_lines), "lines with references show the referenced page")
    h2o = next(t for t in v1["turns"] if "H2O" in t["text"])
    check(h2o["speech"] == "Remember that water is H two O." and h2o["text"] == "Remember that water is H2O." and h2o["delivery"] == "calm",
          "Speech Director: screen keeps H2O, audio says 'H two O', delivery calm")
    sc = setting_line(v0["turns"])
    check(sc["speech"] == sc["text"], "Speech Director: a rewrite that changes the content is rejected")
    check("two friends talking" in sc["text"] and "general" in sc["text"] and "standard" in sc["text"] and "secondary" in sc["text"], f"writer got the chosen style ({sc['text']})")
    aud = e["audio"]
    check(aud and aud["included"] == 4 and list(aud["offsets"]) == ["0", "1", "2", "3"], "episode joined with segment start times")
    check(abs(aud["offsets"]["1"] - aud["offsets"]["0"] - (segs[0]["versions"][0]["duration"] + 0.7)) < 0.05, "segment 2 starts after segment 1 plus the gap")
    check(requests.get(f"{BASE}/files/{s1}/{aud['path']}").status_code == 200, "episode mp3 served")

    # ------------------------------------------------ podcast 2: a specific part, style change, needs
    r = say(s1, "part", "Only the part about guard cells, as teacher and student, simpler please")
    e2 = j(r)["episode"]
    st = wait(s1, ep_done(e2))
    e = ep_of(st, e2)
    check(e["status"] == "done", f"podcast 2 finished ({e['status']}, {e.get('error')})")
    check("previous=1" in e["understanding"], f"interpreter was told about podcast 1 in this session ({e['understanding']})")
    check(e["settings"]["format"] == "teacher_student" and e["roles"]["A_role"] == "Student", "'as teacher and student' in the message changed the format")
    check(e["needs"] == ["use simpler words"], "the student's need was extracted")
    used = {(i[:2], int(i.split("p")[1].split("b")[0])) for s in e["segments"] for i in s["block_ids"]}
    check(used and all(d == "d2" and p in (2, 3) for d, p in used), f"only the pages about guard cells were used ({sorted(used)})")
    t0 = e["segments"][0]["versions"][0]["turns"]
    check(any("Request heard: use simpler words" in t["text"] for t in t0), "the writer received the student's need")
    check(ep_of(st, e1)["status"] == "done" and len(ep_of(st, e1)["segments"]) == 4, "podcast 1 is untouched")

    # ------------------------------------------------ podcast 3: a part that is not there
    e3 = j(say(s1, "part", "about quantum chromodynamics"))["episode"]
    st = wait(s1, ep_done(e3))
    e = ep_of(st, e3)
    check(e["status"] == "not_found" and not e["segments"], "a topic that is not in the PDFs is reported, nothing is made")

    # ------------------------------------------------ regenerate one segment while sending more work
    rr = requests.post(f"{BASE}/api/sessions/{s1}/episodes/{e1}/segments/0/regenerate", json={"note": "Use simpler words"})
    check(rr.status_code == 200, "regenerate accepted")
    check(requests.post(f"{BASE}/api/sessions/{s1}/episodes/{e1}/segments/0/regenerate", json={}).status_code == 409, "not twice at once")
    st = wait(s1, lambda s: len(ep_of(s, e1)["segments"][0]["versions"]) == 2 and ep_of(s, e1)["segments"][0]["status"] == "done"
              and ep_of(s, e1)["audio"]["built"] != aud["built"])
    time.sleep(0.5)
    nv = ep_of(st, e1)["segments"][0]["versions"][1]
    check(nv["note"] == "Use simpler words" and any("As you asked" in t["text"] for t in nv["turns"]), "new version followed the note")
    check(ep_of(st, e1)["audio"]["built"] != aud["built"], "episode rebuilt with the new version")
    check(requests.post(f"{BASE}/api/sessions/{s1}/episodes/{e1}/segments/0/select/1").status_code == 200, "switching back to version 1 works")

    # ------------------------------------------------ session 2: fresh memory
    s2 = requests.post(f"{BASE}/api/sessions").json()["id"]
    upload(s2, b)
    wait(s2, lambda s: all(d["status"] != "analyzing" for d in s["documents"]))
    e21 = j(say(s2, "full", settings={"length": "short", "format": "solo"}))["episode"]
    st2 = wait(s2, ep_done(e21))
    e = ep_of(st2, e21)
    check(e["status"] == "done" and "previous=0" in e["understanding"], f"a new session starts with no memory of other sessions ({e['understanding']})")
    check(all(t["speaker"] == "A" for s in e["segments"] for t in s["versions"][0]["turns"]), "single narrator: one speaker only")
    lst = requests.get(f"{BASE}/api/sessions").json()
    check({x["id"] for x in lst} >= {s1, s2}, "both sessions listed")

    # ------------------------------------------------ session 3: languages
    data_dir = sys.argv[3] if len(sys.argv) > 3 else None
    hi = tmp / "prakash.pdf"
    pdf_hi(hi)
    s3 = requests.post(f"{BASE}/api/sessions").json()["id"]
    upload(s3, hi)
    st3 = wait(s3, lambda s: all(d["status"] != "analyzing" for d in s["documents"]))
    check(st3["documents"][0]["language"] == "hi", f"a Hindi PDF is detected as Hindi ({st3['documents'][0].get('language')})")
    check(sess(s1)["documents"][0]["language"] == "en", "an English PDF is detected as English")
    n_before = len(tts_langs(data_dir)) if data_dir else 0
    e31 = j(say(s3, "full", settings={"length": "short", "language": "hi"}))["episode"]
    st3 = wait(s3, ep_done(e31))
    e = ep_of(st3, e31)
    check(e["status"] == "done" and e["settings"]["language"] == "hi", "choosing Hindi makes a Hindi podcast from a Hindi PDF")
    check("Language hindi" in setting_line(e["segments"][0]["versions"][0]["turns"])["text"], "the writer was told to write in Hindi")
    if data_dir:
        langs = {l.split("\t")[0] for l in tts_langs(data_dir)[n_before:]}
        check(langs == {"hi"}, f"Bodhan received lang=hi for every line ({langs})")
    e32 = j(say(s3, "full", "Same thing but in English please"))["episode"]
    st3 = wait(s3, ep_done(e32))
    e = ep_of(st3, e32)
    check(e["settings"]["language"] == "en" and not e["language_from_pdf"], "writing 'in English' in the message gives an English podcast from the Hindi PDF")
    check("Language english" in setting_line(e["segments"][0]["versions"][0]["turns"])["text"], "the writer was told to write in English")
    e33 = j(say(s3, "part", "about रंध्र", settings={"language": "ta", "length": "short"}))["episode"]
    st3 = wait(s3, ep_done(e33))
    e = ep_of(st3, e33)
    check(e["status"] == "done" and e["settings"]["language"] == "ta", f"choosing Tamil in the Style panel gives a Tamil podcast ({e['status']}, {e['settings'].get('language')})")
    v = e["segments"][0]["versions"][0] if e["segments"] else {"turns": []}
    check(v.get("written_in") == "en" and v.get("language") == "ta", "Tamil is written and checked in English, then translated")
    dsp = e.get("display") or {}
    check(dsp.get("language") == "ta" and tam(dsp.get("title", "")) > 0.9 and tam(dsp.get("understanding", "")) > 0.5
          and all(tam(x) > 0.9 for x in dsp.get("segments", [])) and len(dsp.get("segments", [])) == len(e["segments"]),
          "the title, summary and segment titles are shown in Tamil")
    check(all(tam(k) > 0.9 for kp in dsp.get("key_points", []) for k in kp) and e["title"] and tam(e["title"]) < 0.5,
          "key points too; the agents keep the English originals")
    check(v["turns"] and "Language english" in v["turns"][sline(v["turns"])]["original"], "the writer wrote in English (kept as each line's original)")
    check(all(tam(t["text"]) > 0.9 and t["speech"] == t["text"] for t in v["turns"]), "every line shown and spoken is in Tamil script")
    check(all(t["original"] and t["original"] != t["text"] for t in v["turns"]), "the English original is kept per line")
    check(len(v["turns"][sline(v["turns"])]["text"].split()) == len(v["turns"][sline(v["turns"])]["original"].split()), "a cut-off translation was redone sentence by sentence (nothing lost)")
    if data_dir:
        tl = [l for l in tts_langs(data_dir) if l.startswith("ta\t")]
        check(tl and all(any("\u0b80" <= c <= "\u0bff" for c in l.split("\t")[2]) for l in tl), "Bodhan TTS got lang=ta and Tamil text")
    st3h = sess(s3)
    eh = ep_of(st3h, e31)["segments"][0]["versions"][0]
    check(eh["written_in"] == "hi" and "original" not in eh["turns"][0], "Hindi is still written directly, no translation step")
    check(ep_of(sess(s1), e1)["settings"]["language"] == "en", "the default podcast language is English")

    # ------------------------------------------------ v4: options, pauses, music, opening/closing
    o = requests.get(f"{BASE}/api/options").json()
    check("auto" not in [x["id"] for x in o["languages"]] and o["defaults"]["language"] == "en", "no 'Same as the PDF' option; English by default")
    check([x["id"] for x in o["ages"]] == ["primary", "middle", "secondary", "senior"] and [x["id"] for x in o["depths"]] == ["overview", "standard", "detailed", "custom"]
          and "custom" in [x["id"] for x in o["lengths"]], "age groups, clearer detail levels and a custom length are offered")
    e = ep_of(sess(s1), e1)
    gaps = [b["t0"] - a["t1"] for sg in e["segments"] for v in [cur(sg)] for a, b in zip(v["turns"], v["turns"][1:])]
    check(gaps and all(0.849 <= g <= 1.001 for g in gaps), f"pause between speakers is 0.85-1.0 s (min {min(gaps):.3f}, max {max(gaps):.3f})")
    first, last = cur(e["segments"][0])["turns"], cur(e["segments"][-1])["turns"]
    check(first[0]["text"].startswith("Hello, I am Aarav"), "the podcast opens with the speaker's name and the topic")
    check("Happy learning" in last[-1]["text"] and "see you again" in last[-1]["text"], "the podcast ends with 'happy learning, see you again next time'")
    check(not any("music" in t["text"].lower() or "song" in t["text"].lower() for sg in e["segments"] for t in cur(sg)["turns"]), "no script line mentions music or a song")
    hm = requests.get(f"{BASE}/api/health").json()["music"]
    if hm["intro"] and hm["outro"]:
        a = e["audio"]
        check(a["music"]["intro"] and a["music"]["outro"] and a["speech_start"] == 2.0 and a["offsets"]["0"] == 2.0, "intro music plays 2 s before the first line; offsets account for it")
        lastoff = max(a["offsets"].values())
        check(a["duration"] > lastoff + cur(e["segments"][-1])["duration"] + 0.6 + 2.5, f"outro music follows the goodbye (episode {a['duration']:.1f}s)")

    # ------------------------------------------------ v4: kids, custom length and detail, Audience Editor
    e22 = j(say(s2, "full", settings={"age": "primary", "length": "custom", "minutes": 3, "depth": "custom",
                                       "depth_note": "focus on guard cells", "format": "solo", "language": "en"}))["episode"]
    st2 = wait(s2, ep_done(e22))
    e = ep_of(st2, e22)
    check(e["status"] == "done" and e["settings"]["minutes"] == 3 and len(e["segments"]) == 1, f"custom 3 minutes gives one short segment ({len(e['segments'])})")
    v = cur(e["segments"][0])
    sc2 = setting_line(v["turns"])["text"]
    check("primary" in sc2 and "as requested by the student" in sc2, f"writer got the age group and the custom detail ({sc2})")
    check(any(t["text"].startswith("Okay friends") for t in v["turns"]), "the Audience Editor adapted the wording for children")
    check(v["turns"][0]["text"].startswith("Hello") and "Happy learning" in v["turns"][-1]["text"], "a one-segment podcast has both the hello and the goodbye")

    # ------------------------------------------------ v5: Host Framer, per-turn recording, length
    check(v["turns"][0].get("frame") == "opening" and v["turns"][-1].get("frame") == "closing", "the welcome and goodbye come from the Host Framer")
    check(all(not t.get("frame") for t in v["turns"][1:-1]), "the writer itself does not greet or say goodbye")
    check(len(set(round(t["t0"], 2) for t in v["turns"])) == len(v["turns"]) and all(t["t1"] > t["t0"] for t in v["turns"]), "each speaker turn is one continuous recording")
    e5 = j(say(s2, "full", settings={"format": "teacher_student", "length": "custom", "minutes": 1, "language": "en"}))["episode"]
    st5 = wait(s2, ep_done(e5))
    x = ep_of(st5, e5)
    t5 = cur(x["segments"][0])["turns"]
    check(x["settings"]["minutes"] == 1, "a 1-minute podcast can be chosen")
    check(t5[0]["speaker"] == "B" and t5[0]["text"].startswith("Good morning, students"), f"teacher-student opens like a class ({t5[0]['text'][:40]})")
    check(x["segments"][0]["target_words"] <= 120, f"1 minute gets a small word budget ({x['segments'][0]['target_words']} words)")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import options as _opts
    b2 = _opts.word_budget(_opts.normalize({"length": "custom", "minutes": 2}), 5000, False)["content_words"]
    check(150 <= b2 <= 260, f"2 minutes allows about {b2} words (speech + pauses + welcome fit in 2 minutes)")

    # ------------------------------------------------ v4: Edit the script myself (TTS only)
    k = sline(v["turns"])
    lines = [t["text"] for t in v["turns"]]
    check(requests.post(f"{BASE}/api/sessions/{s2}/episodes/{e22}/segments/0/edit", json={"lines": lines[:-1]}).status_code == 400, "a manual edit must keep every line")
    lines[k] = "This line was edited by hand."
    writer_before = sum("Script Writer" in l["msg"] for l in st2["log"] if l["episode"] == e22)
    r = requests.post(f"{BASE}/api/sessions/{s2}/episodes/{e22}/segments/0/edit", json={"lines": lines})
    check(r.status_code == 200, "manual edit accepted")
    st2 = wait(s2, lambda s: len(ep_of(s, e22)["segments"][0]["versions"]) == 2 and ep_of(s, e22)["segments"][0]["status"] == "done")
    nv = cur(ep_of(st2, e22)["segments"][0])
    check(nv["kind"] == "manual" and nv["turns"][k]["text"] == "This line was edited by hand." and nv["turns"][k]["speech"] == nv["turns"][k]["text"], "the edited line is shown and spoken")
    check(all(a["text"] == b["text"] for i, (a, b) in enumerate(zip(v["turns"], nv["turns"])) if i != k), "the other lines are unchanged")
    writer_after = sum("Script Writer" in l["msg"] for l in st2["log"] if l["episode"] == e22)
    check(writer_after == writer_before and any("Manual edit" in l["msg"] for l in st2["log"]), "a manual edit only re-records audio: no agent ran")

    # ------------------------------------------------ v4: Ask AI to revise a translated (Tamil) segment
    rr = requests.post(f"{BASE}/api/sessions/{s3}/episodes/{e33}/segments/0/regenerate", json={"note": "Use simpler language"})
    check(rr.status_code == 200, "AI revision of a Tamil segment accepted")
    st3 = wait(s3, lambda s: len(ep_of(s, e33)["segments"][0]["versions"]) == 2 and ep_of(s, e33)["segments"][0]["status"] == "done")
    tv = cur(ep_of(st3, e33)["segments"][0])
    check(tv["kind"] == "ai" and tv["written_in"] == "en" and any("As you asked" in (t.get("original") or "") for t in tv["turns"]),
          "AI revision: the agents rewrote the English script (the original carries the change)")
    check(all(tam(t["text"]) > 0.9 for t in tv["turns"]), "and Bodhan translated the revised lines into Tamil before recording")

    # ------------------------------------------------ v4: a PDF in a language the LLMs cannot read
    rl = requests.post(f"{BASE}/api/sessions/{s3}/documents/d1/language", json={"language": "mai"})
    check(rl.status_code == 200, "PDF language can be corrected to Maithili")
    st3 = wait(s3, lambda s: s["documents"][0]["status"] != "analyzing")
    d = st3["documents"][0]
    check(d["status"] == "ready" and d["route"] == "ocr_translate" and d["info"]["ocr_pages"] == [1, 2, 3], f"Maithili PDF read with Bodhan OCR on every page ({d.get('route')}, {d.get('info', {}).get('ocr_pages')})")
    blocks = requests.get(f"{BASE}/files/{s3}/docs/d1/blocks.json").json()
    text_blocks = [b for b in blocks if b["kind"] == "text"]
    check(text_blocks and all(b.get("original") and any("\u0900" <= c <= "\u097f" for c in b["original"]) and b["text"].isascii() for b in text_blocks),
          "each block holds the English translation for the agents and keeps the OCR original")
    e34 = j(say(s3, "full", settings={"language": "en", "length": "short"}))["episode"]
    st3 = wait(s3, ep_done(e34))
    check(ep_of(st3, e34)["status"] == "done", "a podcast is made from the OCR-translated PDF")

    # ------------------------------------------------ v7: final check, instructions in any language, more file types
    tamil_eps = [(sid_, e_) for sid_ in (s1, s2, s3) for e_ in sess(sid_)["episodes"] if (e_.get("settings") or {}).get("language") == "ta" and e_["status"] == "done"]
    sid_t, ep_t = tamil_eps[0]
    v_t = cur(ep_t["segments"][0])
    line21 = next(t_ for t_ in v_t["turns"] if "21" in (t_.get("original") or ""))
    logs_t = " ".join(l["msg"] for l in sess(sid_t)["log"] if l.get("episode") == ep_t["id"])
    check("Final Checker" in logs_t and "translating again" in logs_t, "the Final Checker caught a number changed in translation (21 -> 12)")
    check("21" in line21["text"] and "12" not in line21["text"] and not v_t.get("final_issues"), "the line was translated again and now says 21")
    v_en = cur(ep_of(sess(s1), e1)["segments"][0])
    check(v_en.get("final_issues") == [] and all("12" not in t_["text"] for t_ in v_en["turns"]), "English podcasts: nothing translated, nothing flagged")

    e7 = j(say(s1, "full", "कृपया सरल भाषा में समझाइए, पेज 2 पर ध्यान दें", settings={"language": "en", "length": "custom", "minutes": 1}))["episode"]
    st7 = wait(s1, ep_done(e7))
    x7 = ep_of(st7, e7)
    log7 = " ".join(l["msg"] for l in st7["log"] if l.get("episode") == e7)
    check("your request was in Hindi; translated to English for the agents" in log7, "a Hindi request is translated to English for the agents")
    check("got the English translation and the original words" in x7.get("needs", []), "the agents also get the original words (for matching topics in Hindi PDFs)")
    check(st7["messages"][-1]["text"].startswith("कृपया"), "the student still sees their own words")
    r = requests.post(f"{BASE}/api/sessions/{s1}/episodes/{e7}/segments/0/regenerate", json={"note": "इसे छोटा करो"})
    check(r.status_code == 200, "a change request can be written in Hindi")
    st7 = wait(s1, lambda st: all(s_["status"] == "done" for s_ in ep_of(st, e7)["segments"]) and len(ep_of(st, e7)["segments"][0]["versions"]) == 2)
    log7 = " ".join(l["msg"] for l in st7["log"] if l.get("episode") == e7)
    check("your change request was in Hindi; translated to English" in log7, "the change request was translated for the agents")

    import io, docx as _docx, pptx as _pptx
    s7 = j(requests.post(f"{BASE}/api/sessions"))["id"]
    img = pymupdf.open(); pg_ = img.new_page(width=400, height=300); pg_.draw_rect(pymupdf.Rect(20, 20, 380, 280), color=(0, 0, 0))
    png = pg_.get_pixmap(dpi=72).tobytes("png")
    dd = _docx.Document(); dd.add_paragraph("पौधे अपना भोजन स्वयं बनाते हैं।"); dd.add_paragraph("Roots absorb water from the soil.")
    bd = io.BytesIO(); dd.save(bd)
    pp = _pptx.Presentation()
    for title_, body_ in (("Photosynthesis", "Leaves use sunlight."), ("Roots", "Roots hold the plant.")):
        sl = pp.slides.add_slide(pp.slide_layouts[1]); sl.shapes.title.text = title_; sl.placeholders[1].text = body_
    bp = io.BytesIO(); pp.save(bp)
    files = [("files", ("page_photo.png", png, "image/png")), ("files", ("notes.docx", bd.getvalue(), "application/octet-stream")),
             ("files", ("slides.pptx", bp.getvalue(), "application/octet-stream")), ("files", ("summary.txt", "Plants need light.\n\nThey also need water.".encode(), "text/plain"))]
    check(requests.post(f"{BASE}/api/sessions/{s7}/documents", files=files).status_code == 200, "a photo, a Word file, a PowerPoint and a text file can be uploaded")
    check(requests.post(f"{BASE}/api/sessions/{s7}/documents", files=[("files", ("virus.exe", b"MZ..", "application/octet-stream"))]).status_code == 400, "other file types are refused with a clear message")
    st_ = wait(s7, lambda st: len(st["documents"]) == 4 and all(d_["status"] != "analyzing" for d_ in st["documents"]))
    kinds = {d_["filename"]: (d_.get("kind"), d_["status"], d_["pages"]) for d_ in st_["documents"]}
    check(kinds["page_photo.png"][:2] == ("image", "ready") and kinds["notes.docx"][:2] == ("docx", "ready")
          and kinds["slides.pptx"] == ("pptx", "ready", 2) and kinds["summary.txt"][:2] == ("text", "ready"), f"all four are read ({kinds})")
    def blocks_of(name):
        d_ = next(x for x in st_["documents"] if x["filename"] == name)
        return requests.get(f"{BASE}/files/{s7}/docs/{d_['id']}/blocks.json").json()
    check(any(b["text"] == "पौधे अपना भोजन स्वयं बनाते हैं।" for b in blocks_of("notes.docx")), "Word text reaches the agents exactly as written (Hindi intact)")
    check(any("Roots hold the plant." in b["text"] for b in blocks_of("slides.pptx")), "PowerPoint slide text is read")
    check(len(blocks_of("page_photo.png")) >= 1, "a photo of a page is read like a scanned page")
    check(requests.get(f"{BASE}/files/{s7}/docs/{st_['documents'][1]['id']}/pages/p1.png").status_code == 200, "converted files have page images for the viewer")
    check(st_["title"] == "page_photo", f"the podcast is named after the first file without its extension ({st_['title']})")

    # ------------------------------------------------ v8: revise results, fresh version, numbers, several files, length
    from app import producer as _prod, speech_rates as _sr
    from app.agents import final_checker as _fc, learning_designer as _ld
    from app.documents import _garbled
    logs_t = " ".join(l["msg"] for l in sess(sid_t)["log"] if l.get("episode") == ep_t["id"])
    check("10 lakh is not" not in logs_t and _fc._same_value("1,000,000", 1000000) and not _fc._same_value(21, 12),
          "10 lakh = 1 million is not reported as a number error (values compared in code)")
    v7 = cur(ep_of(st7, e7)["segments"][0])
    check(v7["kind"] == "ai" and (v7.get("request_check") or {}).get("done") in ("yes", "no"), f"a revision records whether the change was made ({v7.get('request_check')})")
    r = requests.post(f"{BASE}/api/sessions/{s1}/episodes/{e7}/segments/0/regenerate", json={"note": ""})
    check(r.status_code == 200, "a segment can be made again from scratch (no instructions)")
    st8 = wait(s1, lambda st: len(ep_of(st, e7)["segments"][0]["versions"]) == 3 and ep_of(st, e7)["segments"][0]["status"] == "done")
    v8 = cur(ep_of(st8, e7)["segments"][0])
    check(v8["kind"] == "ai" and not v8.get("note") and v8["v"] == 3 and v8["turns"][0].get("frame") == "opening", "the fresh version is new, keeps the welcome, and older versions stay")
    seg_ = {"target_words": 200}
    prev_ = [{"text": " ".join(["w"] * 260)}]
    check(_prod._target_for(seg_, "Make it shorter", prev_) < 200 and _prod._target_for(seg_, "Add a real-life example", prev_) > 260
          and _prod._target_for(seg_, None, None) == 200, "a 'shorter' request lowers the budget, 'add an example' raises it")
    rates = json.loads((Path(DATA_DIR_ENV) / "speech_rates.json").read_text()) if DATA_DIR_ENV else {}
    check(rates.get("en", {}).get("n", 0) >= 1 and rates.get("ta", {}).get("n", 0) >= 1, f"the real speaking speed is measured per language ({ {k: round(v['rate'], 2) for k, v in rates.items()} })")
    blocks_ = [{"id": f"d1p1b{i}", "doc": "d1", "page": 1, "text": "word " * 50} for i in range(6)] + [{"id": f"d2p1b{i}", "doc": "d2", "page": 1, "text": "word " * 50} for i in range(4)]
    one_doc = [{"title": "A", "start": 0, "end": 5, "key_points": []}, {"title": "B", "start": 6, "end": 9, "key_points": []}]
    only_first = [{"title": "A", "start": 0, "end": 2, "key_points": []}, {"title": "B", "start": 3, "end": 6, "key_points": []}]
    check(_ld._covers_each(one_doc, blocks_, ["d1", "d2"]) and not _ld._covers_each(only_first, blocks_, ["d1", "d2"]), "a plan that leaves a file out is detected")
    fixed = _ld._split_by_document(blocks_, ["d1", "d2"], 3)
    check(_ld._covers_each(fixed, blocks_, ["d1", "d2"]) and all(len({blocks_[i]["doc"] for i in range(s_["start"], s_["end"] + 1)}) == 1 for s_ in fixed),
          "and re-split so every file gets its own segment(s), never mixing files")
    check(_garbled(["உண்ணேயதானை இரத்க் குழதாய ்ககள் இல்லை ு ு வேர்கள ் நீரை ்உறிஞ்சு ிகின்றன அட்டை ை கள ் இரத்த த்தை ்உறி ஞ்சு ுகின்றன இது ஒரு ்நல்ல"])
          and not _garbled(["தாவரங்கள் உணவைத் தயாரிக்கின்றன. இலைகள் பச்சை நிறத்தில் உள்ளன. வேர்கள் நீரை உறிஞ்சுகின்றன. அட்டைகள் இரத்தத்தை உறிஞ்சுகின்றன. இது ஒரு நல்ல பாடம்."]),
          "a broken Tamil text layer is detected (those pages are read from the image instead)")

    # ------------------------------------------------ v9: voice styles, no length step on revise
    from app import options as _o
    check(_o.bodhan_style("explaining", {"age": "primary", "format": "friends"}) == "children's stories"
          and _o.bodhan_style("explaining", {"age": "secondary", "format": "solo"}) == "single person narration audiobook"
          and _o.bodhan_style("explaining", {"age": "secondary", "format": "friends"}) == "educational lecture"
          and _o.bodhan_style("amazed") == "surprise" and _o.bodhan_style("curious") is None,
          "each delivery maps to a fitting Bodhan style, adjusted for class level and format")
    used = {s_ for d_ in _o.table()["deliveries"].values() for s_ in [d_.get("bodhan_style")]}
    check(not ({"anger", "disgust", "fear"} & used), "anger, disgust and fear are never used")
    e22v = cur(ep_of(sess(s2), e22)["segments"][0])
    check(all(t_.get("style") in ("children's stories", "happy", "", "surprise", "news", "single person narration audiobook") for t_ in e22v["turns"])
          and any(t_.get("style") == "children's stories" for t_ in e22v["turns"]), "a Primary podcast is read in the storyteller voice")
    if data_dir:
        styles = {l.split("\t")[1] for l in tts_langs(data_dir)}
        check("children's stories" in styles and "educational lecture" in styles, f"Bodhan receives the chosen styles ({sorted(styles)})")
    log7 = " ".join(l["msg"] for l in sess(s1)["log"] if l.get("episode") == e7)
    check("Length check" not in log7, "'Revise with AI' has no length step")

    # ------------------------------------------------ v4: interface language
    en = requests.get(f"{BASE}/api/ui/en").json()
    check(en["status"] == "ready" and en["strings"]["cmp.submit"] == "Create podcast" and en["strings"]["opt.ages.primary.label"] == "Primary (Class 1-5)", "English interface text served, option labels included")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests import mocks as _m
    from app import build_ui_text, i18n as _i18n
    _m.install()
    opts = requests.get(f"{BASE}/api/options").json()
    ids = [x["id"] for x in opts["ui_languages"]]
    check(len(ids) == 12 and ids[0] == "en" and "hi" in ids and "as" not in ids, f"12 interface languages offered ({', '.join(ids)})")
    _i18n.ui_file("ta").unlink(missing_ok=True)
    check(not next(x for x in requests.get(f"{BASE}/api/options").json()["ui_languages"] if x["id"] == "ta")["ready"], "a language not built yet is shown as not set up")
    build_ui_text.build("ta")
    t0 = time.time()
    ta = requests.get(f"{BASE}/api/ui/ta").json()
    check(time.time() - t0 < 1 and ta["status"] == "ready" and tam(ta["strings"]["cmp.submit"]) > 0.5, "the saved Tamil interface loads instantly")
    check("{n}" in ta["strings"]["doc.pages"] and "{lang}" in ta["strings"]["ui.ready"], "placeholders survive translation")
    check(build_ui_text.build("ta") == 0, "building again only translates what changed (nothing)")
    check(ta["strings"]["app.name"] == "ஜிக்ஞாசா டாக்ஸ்" and requests.get(f"{BASE}/api/ui/en").json()["strings"]["app.name"] == "Jigyasa Talks",
          "the app name is written in each language's own script (fixed, not machine-translated)")
    import yaml as _y
    names = _y.safe_load(open(Path(__file__).resolve().parent.parent / "config" / "options.yaml", encoding="utf-8"))["app_names"]
    check(names["hi"] == "जिज्ञासा टॉक्स" and set(names) == {x["id"] for x in opts["ui_languages"]}, "every interface language has its own app name (Hindi in Devanagari)")
    check(requests.get(f"{BASE}/api/ui/en").json()["strings"]["ep.open"] == "Play podcast", "'Open player' is now 'Play podcast'")

    hh = requests.get(f"{BASE}/api/health").json()["parallel"]
    check(1 <= hh["peak"] <= 3, f"never more than 3 segments at once (peak {hh['peak']})")

    if KEEP:
        KEEP.write_text(json.dumps({"s1": s1, "s2": s2, "s3": s3, "e1": e1, "e2": e2, "e3": e3, "e21": e21, "e22": e22, "e33": e33}))
    else:
        for sid in (s1, s2, s3):
            requests.delete(f"{BASE}/api/sessions/{sid}")
        check(requests.get(f"{BASE}/api/sessions/{s2}").status_code == 404, "deleting a session removes it")

    print()
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
