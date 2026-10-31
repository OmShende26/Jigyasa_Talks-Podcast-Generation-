"""Check the intro/outro setup:  python -m app.music_check
Prints the ffmpeg version, the music files found, and mixes them with a 5-second test tone into
data/music_check.mp3, reporting every problem."""
import subprocess
from pathlib import Path

from . import config
from .speech import audio


def main():
    """Print the ffmpeg version and music files found, and mix them with a test tone into data/music_check.mp3."""
    ff = audio._bin("ffmpeg")
    try:
        v = subprocess.run([ff, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    except Exception as e:
        v = f"NOT RUNNABLE ({e})"
    print("ffmpeg :", ff, "|", v)
    intro, outro = config.music("intro"), config.music("outro")
    print("intro  :", intro or f"NOT FOUND in {config.ASSETS_DIR}")
    print("outro  :", outro or f"NOT FOUND in {config.ASSETS_DIR}")
    out_dir = config.SESSIONS_DIR.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    tone, out = out_dir / "music_check_tone.mp3", out_dir / "music_check.mp3"
    audio._run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=5", str(tone)])
    msgs = []
    lead = audio.add_music(tone, out, intro, outro, warn=msgs.append)
    for m in msgs:
        print("note   :", m)
    print(f"result : {out}  ({audio.duration(out):.1f} s; tone alone is 5.0 s; speech starts at {lead:.1f} s)")
    print("Listen to it: you should hear the intro, a 5-second beep, then the outro.")
    tone.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
