![TikPatch: upload to TikTok without the quality loss](assets/card.png)

A Claude Code plugin that gets TikTok to post your video at the quality you exported, instead of TikTok's own re-encode (often 540p at about 1 Mbps).

TikPatch changes only the MP4 container of your video. It adds a third of a second of silent audio, and the last frames of that silence are labelled as side channels. Uploaded through TikTok Web, a file patched this way is served to viewers exactly as you uploaded it. Your video and audio data are never re-encoded.

## What it does

1. **Checks your video:** codec, resolution, bitrate, HDR and audio.
2. **Makes a delivery file if needed:** 1080p H.264 at 25 Mbps, iPhone HDR converted to SDR, rotation fixed. An export that's already right is left alone.
3. **Patches it** and confirms the patch is in place.
4. **Tells you how to upload:** TikTok Web on a computer, without a TikTok sound or edits.
5. **Checks the posted video** from its link: did TikTok serve your exact file, or re-encode it?

## The trade-off

A post that keeps full quality this way has **no clickable sound page**. In testing, every way to get a sound page (adding a TikTok sound, uploading through the phone app) made TikTok re-encode the video. Credit the song in your caption instead.

## Install

```bash
claude plugin marketplace add 99Luuk/TikPatch
claude plugin install tikpatch@tikpatch
```

## Use

Ask in your own words, for example:

```
make ~/Movies/edit.mp4 TikTok ready
did TikTok keep my quality? https://www.tiktok.com/@you/video/123
```

You can also call it directly with `/tikpatch:tikpatch`.

## Requirements

TikPatch works in Claude Code, because it reads and writes video files on your computer.

- **Python 3.8 or newer** for the patch (standard library only)
- **ffmpeg and ffprobe** for checking and converting videos
- **yt-dlp**, only for checking a posted video

## What it runs and connects to

- **`scripts/tikpatch.py`**, a readable Python script in this plugin. It reads your video and writes a patched copy next to it. It never overwrites your files and never uploads anything.
- **ffprobe and ffmpeg** on your computer, to read video details and make a delivery file.
- **yt-dlp**, only when you ask to check a posted video. It downloads that public TikTok post from tiktok.com into a `tiktok_check` folder, so it can be compared with your file.
- If a tool is missing, Claude suggests an install command (Homebrew or pip) and runs it only with your OK.

TikPatch doesn't upload your video. You upload it to TikTok yourself.

## Privacy

TikPatch collects, stores and sends no personal data. Your videos stay on your computer. The only network request it makes is the optional download of your own public TikTok post, described above.

## What was tested

In November 2026, every patched video uploaded through TikTok Web passed through untouched, checked frame by frame, from 1080p up to 4K at 60 fps. Uploads through the phone app, posts with a TikTok sound and unpatched files were all re-encoded. These results come from one test account.

## Caveats

- TikPatch relies on a quirk in how TikTok processes uploads. TikTok can change that at any time.
- With passthrough, viewers get your exact file with no lower-quality fallback. Keep exports around 1080p and 20 to 30 Mbps so they stream smoothly. TikPatch does this by default.
- TikPatch is an independent project. It is not affiliated with or endorsed by TikTok or ByteDance.

## License

MIT
