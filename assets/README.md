# Intro and outro music

Put your two music files here, with exactly these names:

- `intro.mp3`: plays at the start of every finished podcast. The first speaker starts after
  2 seconds while the music fades out underneath (INTRO_LEAD_SEC and INTRO_FADE_SEC in .env).
- `outro.mp3`: plays after the goodbye, to the end of the file (OUTRO_GAP_SEC sets the pause before it).

Any common audio format works if you point to it in .env instead:
INTRO_MUSIC=assets/my_intro.wav and OUTRO_MUSIC=assets/my_outro.wav.
MUSIC_VOLUME (default 0.6) sets how loud the music is compared to the voices.

Leave a file out to have no intro or no outro. Music is added only to the complete podcast
(the "Download MP3" file and the full player), not to single segments while they are being made.
New files are used the next time a podcast is put together; to add music to an existing podcast,
regenerate or edit any one of its segments.
