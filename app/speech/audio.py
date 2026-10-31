"""ffmpeg helpers. All clips are normalised to one format so durations add up exactly."""
import os
import shutil
import subprocess
import wave
from pathlib import Path

from .. import config  # noqa: F401  (importing config loads .env, so FFMPEG_PATH / FFPROBE_PATH work)

SR = 24000


def _bin(name):
    """FFMPEG_PATH / FFPROBE_PATH from .env, else whatever is on PATH."""
    return os.getenv(f"{name.upper()}_PATH", "").strip() or shutil.which(name) or name


def _run(cmd):
    cmd = [_bin(cmd[0])] + cmd[1:]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg was not found. Add its folder to PATH and restart the terminal, "
            "or set FFMPEG_PATH and FFPROBE_PATH in .env"
        )
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr[-500:]}")
    return p


_TRIM = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02,"
         "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.04,areverse")


def to_norm_wav(src: Path, dst: Path):
    """Any clip -> 24 kHz mono PCM, with the engine's own leading/trailing silence removed (TRIM_SILENCE),
    so the pauses we insert are the only pauses."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if config.TRIM_SILENCE:
        cmd += ["-af", _TRIM]
    _run(cmd + ["-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    if config.TRIM_SILENCE and wav_seconds(dst) < 0.05:  # all "silence" (very quiet clip): keep it untrimmed
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])


def wav_seconds(path: Path) -> float:
    """Length of a WAV file in seconds."""
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _silence(ms, dst: Path):
    _run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=mono",
        "-t", f"{max(ms, 20) / 1000:.3f}", "-c:a", "pcm_s16le", str(dst),
    ])


def duration(path: Path) -> float:
    """Length of any audio file in seconds (ffprobe); 0.0 if unreadable."""
    try:
        p = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
        return round(float(p.stdout.strip()), 3)
    except (RuntimeError, ValueError):
        return 0.0


def stitch(items, out_mp3: Path):
    """items: list of (normalised wav path, silence_ms_before). Writes one mp3."""
    work = out_mp3.parent / f".work_{out_mp3.stem}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        sil, lines = {}, []
        for path, gap in items:
            if gap > 0:
                if gap not in sil:
                    sil[gap] = work / f"sil{gap}.wav"
                    _silence(gap, sil[gap])
                lines.append(f"file '{sil[gap].resolve().as_posix()}'")
            lines.append(f"file '{Path(path).resolve().as_posix()}'")
        listing = work / "list.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c:a", "libmp3lame", "-q:a", "4", str(out_mp3),
        ])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def concat_mp3(paths, out_mp3: Path, gap_ms: int):
    """Join finished segment mp3s with a gap between them."""
    work = out_mp3.parent / f".work_{out_mp3.stem}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        sil = work / "gap.wav"
        _silence(gap_ms, sil)
        inputs = []
        for i, p in enumerate(paths):
            if i:
                inputs.append(sil)
            inputs.append(Path(p))
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for p in inputs:
            cmd += ["-err_detect", "ignore_err", "-i", str(p)]
        fc = ";".join(f"[{i}:a]aformat=sample_rates={SR}:channel_layouts=mono[a{i}]" for i in range(len(inputs)))
        fc += ";" + "".join(f"[a{i}]" for i in range(len(inputs))) + f"concat=n={len(inputs)}:v=0:a=1[out]"
        cmd += ["-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", str(out_mp3)]
        _run(cmd)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _clean_music(src: Path, dst: Path, trim_start=False):
    """Decode a music file to plain 44.1 kHz stereo WAV: drops cover art / extra streams and skips damaged frames.
    trim_start: also cut the silence / near-silent fade-in at the start (many songs begin quietly, and only the
    first few seconds of the intro are used)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
           "-i", str(src), "-map", "0:a:0", "-vn"]
    if trim_start:
        cmd += ["-af", "silenceremove=start_periods=1:start_threshold=-35dB:start_silence=0.05"]
    _run(cmd + ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(dst)])
    if wav_seconds(dst) < 0.2:
        raise RuntimeError("no usable audio in the file")


def rms_db(path: Path, start=0.0, end=None) -> float:
    """Average loudness (dB) of a stretch of a file; -100 for silence."""
    cmd = ["ffmpeg", "-v", "info", "-nostats", "-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    p = _run(cmd + ["-i", str(path), "-af", "astats=metadata=0:measure_perchannel=none", "-f", "null", "-"])
    import re as _re
    m = _re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", p.stderr)
    return -100.0 if not m or m[-1] == "-inf" else float(m[-1])


def add_music(speech_mp3: Path, out_mp3: Path, intro: Path = None, outro: Path = None, warn=None):
    """Like _add_music, but a music file that cannot be read is left out (with a warning) instead of failing."""
    work = out_mp3.parent / f".music_{out_mp3.stem}"
    work.mkdir(parents=True, exist_ok=True)
    warn = warn or (lambda m: None)
    try:
        clean = {}
        for name, src in (("intro", intro), ("outro", outro)):
            if src and Path(src).exists():
                try:
                    clean[name] = work / f"{name}.wav"
                    _clean_music(Path(src), clean[name], trim_start=(name == "intro"))
                    if name == "intro":
                        lvl = rms_db(clean[name], 0, config.INTRO_LEAD_SEC + config.INTRO_FADE_SEC)
                        if lvl < -40:
                            warn(f"The first seconds of the intro music are very quiet ({lvl:.0f} dB), so it may be hard to hear. "
                                 "Use a part of the song that starts loud, or raise MUSIC_VOLUME / INTRO_LEAD_SEC in .env")
                except Exception as e:
                    clean.pop(name, None)
                    warn(f"The {name} music ({Path(src).name}) could not be read and was left out: {str(e)[-200:]}")
        try:
            lead = _add_music(speech_mp3, out_mp3, clean.get("intro"), clean.get("outro"))
            if lead > 0:
                warn(f"Intro check: the first {lead:.1f} s of the podcast play the intro at {rms_db(out_mp3, 0, lead):.0f} dB")
            return lead
        except Exception as e:
            warn(f"Adding the music failed, so the podcast was saved without it: {str(e)[-200:]}")
            shutil.copyfile(speech_mp3, out_mp3)
            return 0.0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _add_music(speech_mp3: Path, out_mp3: Path, intro: Path = None, outro: Path = None):
    """Episode with intro/outro music, using only filters that every ffmpeg version has
    (no amix normalize / adelay all=1, which older builds lack). intro/outro are clean 44.1 kHz stereo WAVs."""
    lead = config.INTRO_LEAD_SEC if intro else 0.0
    fade, vol = max(config.INTRO_FADE_SEC, 0.01), config.MUSIC_VOLUME
    work = out_mp3.parent / f".mix_{out_mp3.stem}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        body = work / "body.wav"
        # speech as 44.1 kHz stereo WAV, preceded by `lead` seconds of silence
        sp = work / "speech.wav"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(speech_mp3), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(sp)])
        if intro:
            pre = work / "pre.wav"
            _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                  "-t", f"{lead:.3f}", "-c:a", "pcm_s16le", str(pre)]) if lead > 0 else None
            spd = work / "speech_d.wav"
            if lead > 0:
                (work / "l1.txt").write_text(f"file '{pre.resolve().as_posix()}'\nfile '{sp.resolve().as_posix()}'\n", encoding="utf-8")
                _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "l1.txt"), "-c", "copy", str(spd)])
            else:
                spd = sp
            # intro: first lead+fade seconds, fading out, quieter; padded with silence; summed with the speech
            im = work / "intro_m.wav"
            _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(intro),
                  "-af", f"atrim=0:{lead + fade:.3f},afade=t=out:st={lead:.3f}:d={fade:.3f},volume={vol}",
                  "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(im)])
            _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(spd), "-i", str(im), "-filter_complex",
                  "[1:a]apad[m];[0:a][m]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3[o]",
                  "-map", "[o]", "-c:a", "pcm_s16le", str(body)])
        else:
            body = sp
        if outro:
            om = work / "outro_m.wav"
            _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(outro), "-af", f"afade=t=in:d=0.5,volume={vol}",
                  "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(om)])
            body_len, outro_len = wav_seconds(body), wav_seconds(om)
            if config.OUTRO_OVERLAP_SEC > 0:
                # the outro starts OUTRO_OVERLAP_SEC before the speech ends, under the goodbye
                start = max(body_len - config.OUTRO_OVERLAP_SEC, body_len - outro_len + 0.5, 0.0)
                pre = work / "opre.wav"
                _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                      "-t", f"{max(start, 0.02):.3f}", "-c:a", "pcm_s16le", str(pre)])
                (work / "l3.txt").write_text(f"file '{pre.resolve().as_posix()}'\nfile '{om.resolve().as_posix()}'\n", encoding="utf-8")
                od = work / "outro_d.wav"
                _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "l3.txt"), "-c", "copy", str(od)])
                mixed = work / "mixed.wav"
                _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(body), "-i", str(od), "-filter_complex",
                      "[0:a]apad[b];[b][1:a]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3[o]",
                      "-map", "[o]", "-c:a", "pcm_s16le", str(mixed)])
                parts = [mixed]
            else:
                gap = work / "gap.wav"
                _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                      "-t", f"{max(config.OUTRO_GAP_SEC, 0.02):.3f}", "-c:a", "pcm_s16le", str(gap)])
                parts = [body, gap, om]
        else:
            parts = [body]
        (work / "l2.txt").write_text("".join(f"file '{x.resolve().as_posix()}'\n" for x in parts), encoding="utf-8")
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "l2.txt"),
              "-c:a", "libmp3lame", "-q:a", "3", str(out_mp3)])
        return lead
    finally:
        shutil.rmtree(work, ignore_errors=True)
