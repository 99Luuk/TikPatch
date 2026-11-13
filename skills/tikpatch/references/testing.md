# What was tested, and how to re-test

Read this when a patched post got re-encoded for no clear reason, or when the user wants to
check that the method still works.

## Results (November 2026, TikTok Web unless noted)

| Upload | Result |
|---|---|
| SL/SR variants (`tail-pce`, `full`, `full` without marker) | passthrough every time |
| Same patched file through the TikTok app (Android), even with no sound | re-encoded |
| Patched file with any TikTok sound added (web or app) | re-encoded |
| Unpatched control, `tail`, `dup`, `moov-only`, `tail-dup` | re-encoded (~540p, 0.7-1.5 Mbps) |
| Clean audio as track 1, SL/SR track 2 (to keep a sound page) | re-encoded, TikTok used track 1 |
| Video stream padded with thousands of dummy samples | post held on "Only me", couldn't publish |

Also passed through: real edits from an editor with music mixed in; 1080p60 at 25-30 Mbps,
1440p60 at 40, 4K60 at 50. A 4K file at 275 Mbps passed through but was unplayable on a phone.

Quick signal: a passthrough post has no sound artist (`yt-dlp --print "%(artist)s"` gives
`NA`). The results were the same 2 minutes and 1 hour after posting.

These come from one test account, one upload per variant per round. Treat
them as strong evidence, not proof.

## Re-testing with the matrix

```bash
python3 "$SKILL_DIR/scripts/tikpatch.py" matrix clip1.mp4 clip2.mp4 clip3.mp4 clip4.mp4 clip5.mp4 clip6.mp4 clip7.mp4 clip8.mp4
```

This writes `00_control.mp4` through `07_full-nomarker.mp4` into a `<first clip>_tests` folder,
one variant per clip. Give it different clips: TikTok may treat eight uploads of the same video
as duplicates and reuse one result. Each input must already meet the step 2 checks.

| File | Tests | Expected (Nov 2026) |
|---|---|---|
| 00_control | untouched original | re-encoded |
| 01_full | tail + SL/SR + duplicate audio track | passthrough |
| 02_moov-only | only moves the index to the end | re-encoded |
| 03_tail | silent audio tail only | re-encoded |
| 04_tail-pce | tail + SL/SR switch (the default) | passthrough |
| 05_tail-dup | tail + duplicate track, no SL/SR | re-encoded |
| 06_dup | duplicate track only | re-encoded |
| 07_full-nomarker | full, without the tikpatch marker box | passthrough |

The user posts each one through TikTok Web with the same settings, using the file name as the
caption so links can be matched to files. Then check every link with the quick `artist` print
and `compare` against its matrix file. If 01, 04 and 07 are re-encoded too, TikTok has changed
its pipeline and the method no longer works. Tell the user plainly.
