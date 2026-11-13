# Delivery file commands

Use these to fix a file before patching (SKILL.md step 2). Name the output
`<input name>_tiktok.mp4` next to the input unless the user says otherwise.

- [Full delivery encode, SDR source](#full-delivery-encode-sdr-source)
- [HDR sources](#hdr-sources-hlg-or-pq)
- [Audio or container only](#audio-or-container-only-video-copied)
- [1440p and 4K](#1440p-and-4k-only-when-asked)

## Full delivery encode, SDR source

```bash
ffmpeg -i INPUT -map 0:v:0 -map 0:a:0 \
  -vf "scale='if(gt(iw,ih),-2,1080)':'if(gt(iw,ih),1080,-2)':flags=lanczos,format=yuv420p" \
  -fps_mode cfr -r FPS \
  -c:v libx264 -profile:v high -preset slow -b:v 25M -maxrate 30M -bufsize 50M \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a aac -b:a 320k -ar 48000 -ac 2 \
  -movflags +faststart -write_tmcd 0 OUTPUT_tiktok.mp4
```

- The scale expression sets the short side to 1080 and keeps the aspect ratio, for portrait and
  landscape. ffmpeg rotates phone footage upright during a normal software decode, so the
  expression sees the displayed orientation. Use it only to scale down; smaller sources get
  patched as is.
- `FPS`: the source's nominal rate (24, 25, 30, 50 or 60), at most 60. Phone footage is often
  variable frame rate (`avg_frame_rate` like `2925000/49927`, about 58.6): round to the nearest
  standard rate.
- Several audio tracks: change `0:a:0` to the one the user picked.
- No audio track: tikpatch needs one, and a silent track is fine. Add
  `-f lavfi -i anullsrc=r=48000:cl=stereo` after the first input, map `1:a:0` instead of
  `0:a:0`, and add `-shortest`.
- 10-bit SDR sources are converted to 8-bit by `format=yuv420p`; nothing extra needed.

Verified on a Samsung 4K HEVC clip (rotation -90, variable frame rate): upright 1080x1920 H.264
High, 60 fps constant, bt709, no rotation flag left.

## HDR sources (HLG or PQ)

ffprobe shows `color_transfer=arib-std-b67` (HLG: iPhone default, many Androids) or
`smpte2084` (PQ / HDR10). Tone-map to SDR bt709; HDR uploaded as is looks wrong on most
phones. See which route this ffmpeg supports:

```bash
ffmpeg -hide_banner -filters | grep -E " (zscale|scale_vt) "
```

### Route A: zscale (most Linux and Windows builds)

```bash
ffmpeg -i INPUT -map 0:v:0 -map 0:a:0 \
  -vf "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,scale='if(gt(iw,ih),-2,1080)':'if(gt(iw,ih),1080,-2)':flags=lanczos" \
  -fps_mode cfr -r FPS \
  -c:v libx264 -profile:v high -preset slow -b:v 25M -maxrate 30M -bufsize 50M \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a aac -b:a 320k -ar 48000 -ac 2 \
  -movflags +faststart -write_tmcd 0 OUTPUT_tiktok.mp4
```

This is the standard ffmpeg tone-map chain, not verified on the machine this skill was written
on (its ffmpeg has no zscale), so check a frame. If zscale complains about an unknown input
transfer, give it explicitly in the first zscale: `tin=arib-std-b67:pin=bt2020:min=bt2020nc`
for HLG, `tin=smpte2084:pin=bt2020:min=bt2020nc` for PQ. It is slow on 4K; that's expected.

### Route B: macOS VideoToolbox (Homebrew ffmpeg has no zscale)

```bash
ffmpeg -noautorotate -display_rotation 0 -hwaccel videotoolbox -hwaccel_output_format videotoolbox_vld -i INPUT \
  -map 0:v:0 -map 0:a:0 \
  -vf "scale_vt=w=W:h=H:color_matrix=bt709:color_primaries=bt709:color_transfer=bt709,hwdownload,format=p010le,ROTATE,format=yuv420p" \
  -fps_mode cfr -r FPS \
  -c:v libx264 -profile:v high -preset slow -b:v 25M -maxrate 30M -bufsize 50M \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a aac -b:a 320k -ar 48000 -ac 2 \
  -movflags +faststart -write_tmcd 0 OUTPUT_tiktok.mp4
```

With `-noautorotate`, frames arrive in their stored orientation, so fill in by hand:

- `W:H`: the stored width and height scaled so the short side is 1080 (stored 3840x2160 →
  `w=1920:h=1080`).
- `ROTATE`, from ffprobe's `rotation`: `-90` → `transpose=clock`; `90` → `transpose=cclock`;
  `180` or `-180` → `hflip,vflip`; none or `0` → delete `ROTATE,` from the chain.
- `-display_rotation 0` clears the rotation flag in the output, so players don't rotate the
  already-rotated frames a second time. Keep both rotation flags.

Rotation is done by hand here because hardware decoding combined with ffmpeg's automatic
rotation has produced wrongly rotated or mirrored frames in full encodes. Always check a frame.

Verified on an iPhone 4K60 HLG HEVC clip (rotation -90): upright 1080x1920, natural colors
matching a software-decoded reference, no rotation flag left, patched fine.

### Neither route available

Mac: `brew install ffmpeg` includes `scale_vt`. Linux and Windows: a full build with zimg has
`zscale` (most distro packages, gyan.dev "full", BtbN). Don't upload HDR untouched instead.

## Audio or container only (video copied)

Use these when the video itself already passes the step 2 checks. The video stream is copied,
so there's no quality loss.

Remux a `.mov` or fragmented MP4 whose streams are fine:

```bash
ffmpeg -i INPUT -map 0:v:0 -map 0:a:0 -c copy -movflags +faststart OUTPUT.mp4
```

Fix the audio (mono, HE-AAC, PCM, MP3, other sample rates, or picking one of several tracks):

```bash
ffmpeg -i INPUT -map 0:v:0 -map 0:a:N -c:v copy -c:a aac -b:a 320k -ar 48000 -ac 2 -movflags +faststart OUTPUT.mp4
```

Add a silent track when there is no audio:

```bash
ffmpeg -i INPUT -f lavfi -i anullsrc=r=48000:cl=stereo -map 0:v:0 -map 1:a:0 -c:v copy \
  -c:a aac -b:a 128k -ar 48000 -ac 2 -shortest -movflags +faststart OUTPUT.mp4
```

## 1440p and 4K (only when asked)

Same full encode, with the short side and bitrate changed:

| Tier | Short side (replace 1080 in the scale expression) | Bitrate flags |
|---|---|---|
| 1440p | 1440 | `-b:v 40M -maxrate 48M -bufsize 80M` |
| 4K | 2160 | `-b:v 50M -maxrate 60M -bufsize 100M` |

Both passed through at 60 fps in testing and looked the same as 1080p on a phone. Passthrough
has no fallback tier, so viewers on slower connections are more likely to stutter. Tell the
user that when they ask for these.
