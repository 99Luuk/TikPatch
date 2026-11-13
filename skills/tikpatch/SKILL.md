---
name: tikpatch
description: Get TikTok to post a video at full quality instead of re-encoding it (TikTok often cuts uploads to 540p at about 1 Mbps). Checks the video, makes a proper delivery file with ffmpeg when needed (1080p H.264, SDR, tone-maps iPhone HDR), patches it with the bundled tikpatch.py (the "SL/SR quality method" container patch that tools like TikQuick and CompressBase use), gives the TikTok Web upload rules, and checks a posted TikTok link for passthrough vs re-encode. Use this whenever someone wants to export, prepare or upload a video for TikTok, says TikTok makes their videos blurry or compressed, mentions tikpatch, passthrough, SL/SR, TikQuick or CompressBase, or asks whether a TikTok post was re-encoded, even if they only say "make this TikTok ready".
compatibility: Needs local file and shell access (Claude Code). python3 3.8+ is required; ffmpeg/ffprobe for checking and encoding; yt-dlp only for checking a posted video.
---

# tikpatch

TikTok re-encodes almost every upload, often down to 540p at around 1 Mbps. The bundled
`scripts/tikpatch.py` rewrites only the MP4 container of a video, never the video or audio
itself. It appends about 0.35 s of silent AAC frames, and the last few of those declare the
stereo pair as Side Left / Side Right. Uploaded through TikTok Web, a file patched this way is
served to viewers byte-for-byte as uploaded. In tests (November 2026), every patched TikTok Web
upload passed through, including on an account whose normal uploads were capped at 540p.

Because TikTok then serves the file exactly, with no lower-quality fallback versions, the export
*is* the final quality. So most of the work is making the right file before the patch, and
checking the post afterwards.

`SKILL_DIR` below means the folder this SKILL.md is in.

## Say this once, up front

A passthrough post has **no clickable sound page**. That is inherent: in testing, every route
to a clickable sound (adding a TikTok sound, uploading through the app, swapping audio tracks)
meant TikTok re-encoded the video. Suggest crediting the song in the caption instead. If the
user cares more about the sound page than the quality for a given post, don't patch that one.

## Tools

Check with `command -v python3 ffmpeg ffprobe yt-dlp`.

- **python3 3.8+**: required. tikpatch uses only the standard library.
- **ffmpeg / ffprobe**: for checking and encoding. Without them you can still patch a file the
  user says is a finished H.264 export, but tell them it wasn't checked.
- **yt-dlp**: only for step 5. Mac: `brew install yt-dlp`; elsewhere `python3 -m pip install --user yt-dlp`.

Install things only with the user's OK.

## 1. Probe the input

```bash
ffprobe -v error -show_entries format=format_name,duration,bit_rate:stream=index,codec_type,codec_name,profile,width,height,avg_frame_rate,bit_rate,pix_fmt,color_transfer,color_primaries,channels,sample_rate:stream_side_data=rotation -of json INPUT
```

Extra data streams (iPhones add several) are harmless; the encode drops them.

## 2. Decide: patch as is, or make a delivery file first

Re-encoding costs a generation of quality, so only do it when the file has a problem that the
patch would deliver straight to viewers. Patch as is when every row holds:

| Check | Ready to patch | Why |
|---|---|---|
| Video codec | H.264 | the only codec verified to pass through |
| Color | SDR: transfer `bt709` or not set | HDR (`smpte2084` = PQ, `arib-std-b67` = HLG, iPhone default) is served unconverted and looks wrong on most phones |
| Resolution | short side 1080 | see below |
| Video bitrate | 10-60 Mbps | no fallback tier: above ~60 Mbps viewers stutter and buffer (a 275 Mbps upload was unplayable) |
| Frame rate | 60 or less | |
| Audio | exactly one AAC-LC stereo track | tikpatch refuses anything else |
| Container | MP4, not fragmented | tikpatch refuses MOV audio descriptions and fragmented MP4 |

Resolution: 1080p at 30 Mbps, 1440p at 40 and 4K at 50 all passed through and looked identical
on a phone, but the bigger files are harder to stream with no fallback. Default to 1080p; keep
1440p or 4K only when the user asks for it.

When rows fail, fix only what is needed. Exact commands are in `references/encoding.md`:

- **Only audio or container is off** (mono, HE-AAC, PCM, no audio, several audio tracks, `.mov`,
  fragmented): keep the video as is (`-c:v copy`) and fix the audio or remux.
- **Anything about the video is off**: full delivery encode. H.264 High, short side 1080,
  25 Mbps, bt709 SDR (tone-map HDR first), constant frame rate, AAC-LC 320k 48 kHz stereo,
  `+faststart`.
- **Several audio tracks**: ask which one to keep unless it's obvious (e.g. one is silent).

Re-encoding can't fix a weak source. If the source is below 1080p or below about 10 Mbps,
upscaling or raising the bitrate adds no detail: patch it as is and tell the user the source
limits the quality.

Tell the user in one line what you'll do and why, for example: "It's 4K iPhone HDR, so I'm
making a 1080p SDR delivery file first; uploaded as is it would look washed out." Write new
files next to the input (or where the user says) and never overwrite the original.

After an encode, check the delivery file before patching: ffprobe it (codec, 1080 short side
in the right orientation, bt709, one AAC stereo track), and for rotated or HDR sources grab one
frame (`ffmpeg -ss 1 -i DELIVERY.mp4 -frames:v 1 -vf scale=360:-2 check.jpg`) and look at it.
It should be upright with natural colors, not grey or washed out. Rotation mistakes are easy to
make with hardware decoding and invisible in ffprobe.

## 3. Patch

```bash
python3 "$SKILL_DIR/scripts/tikpatch.py" patch DELIVERY.mp4
```

This writes `DELIVERY_tail-pce.mp4`. The default variant is the right one for real posts; the
other variants exist only for re-testing (`references/testing.md`). tikpatch refuses to
overwrite files, refuses files it already patched, and explains any other refusal with a fix
command. Follow that rather than working around it.

Confirm with `python3 "$SKILL_DIR/scripts/tikpatch.py" inspect DELIVERY_tail-pce.mp4`: it must
show `SL/SR side-channel desc: yes`. Then tell the user which file to upload and its
resolution, frame rate, bitrate and size.

## 4. Upload rules

The user does the upload. Give them these:

- Upload the `_tail-pce.mp4` file on **TikTok Web, from a computer**. Not the phone app: the
  app rebuilds the audio before uploading, which strips the patch. Every app upload in testing
  was re-encoded, even with no sound added.
- Don't add a TikTok sound, and don't trim, crop, add text, stickers, effects or filters in
  TikTok's editor. Caption, hashtags, cover and privacy settings are fine.
- Don't pass the file through anything that recompresses it (messaging apps, some cloud
  uploads) on the way to the upload.
- Keep the patched file: the check in step 5 needs it.

## 5. Check a posted video

When the user shares the TikTok link. Quick check, no download:

```bash
yt-dlp --print "%(artist)s | %(format_id)s | %(width)sx%(height)s" "TIKTOK_URL"
```

- artist `NA` or `None`, and the uploaded resolution → passthrough (no sound page exists).
- artist set to the uploader's name, or formats at 540p-1080p in h264/bytevc1 → re-encoded.

Definitive check, comparing the actual video frames:

```bash
mkdir -p tiktok_check && yt-dlp -o "tiktok_check/%(id)s.%(ext)s" "TIKTOK_URL"
python3 "$SKILL_DIR/scripts/tikpatch.py" compare UPLOADED_tail-pce.mp4 tiktok_check/ID.mp4
```

`PASSTHROUGH` means TikTok serves the exact frames. Compare against the exact file that was
uploaded: any other file reads as `RE-ENCODED` even when the post passed through. If that file
is gone, run `inspect` on the download instead: an intact SL/SR side-channel means passthrough,
because TikTok's own encodes never keep it.

If yt-dlp says the video is unavailable or "Your IP address is blocked", TikTok isn't serving
that post publicly. Either the post is private, "Only me" or held for review, or the whole
account is private or restricted. Ask the user to check both. If links from other accounts
fail too, yt-dlp is probably outdated (TikTok breaks it often): update it with
`brew upgrade yt-dlp` or `python3 -m pip install -U yt-dlp`.

Results were the same 2 minutes and 1 hour after posting, so the check can run any time. If a
post was re-encoded, look for causes in this order: uploaded through the app; a sound or edit
was added in TikTok; the unpatched file was uploaded; TikTok changed its pipeline (re-test with
the matrix in `references/testing.md`).

Report what the tool output says. Never call a post passthrough without a check.

## Stay on the proven path

- Use tikpatch's `patch` command, not hand-made container edits. Other tricks were tested and
  failed: an audio-track swap was re-encoded, and video streams padded with dummy samples (a
  trick from another tool) got four posts held on "Only me", with no way to publish them. An
  improvised variation risks the user's posts and account.
- Don't edit `tikpatch.py`. It was checked across dozens of uploads.
- This relies on a quirk in TikTok's pipeline that TikTok can change any day. Say so if the
  user is planning around it.
