#!/usr/bin/env python3
"""tikpatch: rebuild the "SL/SR quality method" MP4 layout locally, and inspect results.

Commands
  patch   IN.mp4 [OUT.mp4] [--variant NAME] [--label TEXT]   default variant: tail-pce
  matrix  CLIP.mp4 [CLIP2.mp4 ...]     writes every test variant plus a control copy
  inspect FILE.mp4 [FILE2.mp4 ...]     layout, codecs, bitrates, encoder tags, verdict
  compare EXPORT.mp4 DOWNLOAD.mp4      byte-level check: passthrough or re-encoded

The video and audio samples are never re-encoded or modified. The patch only
rewrites the MP4 container:
  * the original 'moov' is renamed to 'free' in place and a new 'moov' goes at the end
  * a short tail of silent AAC frames (~0.35 s) is appended in its own 'mdat'
  * the last 8 silent frames switch to a second sample description that declares the
    stereo pair as Side Left / Side Right (AAC program config element)
  * a clean duplicate of the audio track is added ('full' variant only)

Tested 2026-11 (29 uploads): the SL/SR switch is what matters. Variants with it (full,
tail-pce) passed through 12/12 on TikTok Web; tail, dup, moov-only, the control, app uploads
and posts with a TikTok sound were all re-encoded (0/17).

Input requirements: a normal (non-fragmented) MP4 with exactly one AAC-LC stereo
audio track. Most editor exports (Premiere, Resolve, CapCut, AE/AME) qualify.
Pure standard library, Python 3.8+.
"""
import argparse
import copy
import os
import re
import shutil
import struct
import sys
import tempfile

VERSION = '1.1'
CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'dinf'}
# One raw AAC-LC frame of digital silence: a channel pair element with no spectral data.
SILENT_FRAME = bytes([0x21, 0x10, 0x04, 0x60, 0x8C, 0x1C])
SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]
SWITCH_FRAMES = 8  # silent frames at the very end that use the SL/SR description

VARIANTS = {
    # name: (tail, pce, dup)
    'full': (True, True, True),
    'moov-only': (False, False, False),
    'tail': (True, False, False),
    'tail-pce': (True, True, False),
    'tail-dup': (True, False, True),
    'dup': (False, False, True),
}
VARIANT_HELP = {
    'full': 'everything (matches the public tools)',
    'moov-only': 'only moves the index to the end',
    'tail': 'silent tail only',
    'tail-pce': 'silent tail + SL/SR switch, no duplicate track',
    'tail-dup': 'silent tail + duplicate track, no SL/SR switch',
    'dup': 'duplicate audio track only',
}
ALLOWED_AUDIO_STBL = {b'stsd', b'stsz', b'stsc', b'stts', b'stco', b'co64', b'ctts', b'stss',
                      b'stps', b'sdtp', b'sbgp', b'sgpd', b'free', b'skip'}


class PatchError(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise PatchError(msg)


def u16(b, o): return struct.unpack_from('>H', b, o)[0]
def u32(b, o): return struct.unpack_from('>I', b, o)[0]
def u64(b, o): return struct.unpack_from('>Q', b, o)[0]
def p32(*vals): return b''.join(struct.pack('>I', v) for v in vals)


def make_box(btype, body):
    n = len(body) + 8
    if n > 0xFFFFFFFF:
        return struct.pack('>I4sQ', 1, btype, n + 8) + body
    return struct.pack('>I4s', n, btype) + body


class Box:
    __slots__ = ('type', 'data', 'children')

    def __init__(self, btype, data=b'', children=None):
        self.type, self.data, self.children = btype, data, children

    def find(self, t):
        return next((c for c in self.children if c.type == t), None)

    def get(self, t):
        c = self.find(t)
        need(c is not None, 'Missing MP4 box: ' + t.decode('latin1'))
        return c

    def serialize(self):
        if self.children is not None:
            return make_box(self.type, b''.join(c.serialize() for c in self.children))
        return make_box(self.type, self.data)


def parse_boxes(buf, depth=0):
    need(depth < 16, 'MP4 nesting too deep')
    out, at = [], 0
    while at < len(buf):
        need(at + 8 <= len(buf), 'Truncated MP4 box')
        size, t = struct.unpack_from('>I4s', buf, at)
        hl = 8
        if size == 1:
            need(at + 16 <= len(buf), 'Truncated MP4 box')
            size, hl = u64(buf, at + 8), 16
        elif size == 0:
            size = len(buf) - at
        need(hl <= size and at + size <= len(buf), 'Invalid MP4 box size')
        body = bytes(buf[at + hl:at + size])
        out.append(Box(t, children=parse_boxes(body, depth + 1)) if t in CONTAINERS else Box(t, data=body))
        at += size
    return out


def scan_top(f, fsize):
    boxes, at = [], 0
    while at < fsize:
        f.seek(at)
        h = f.read(16)
        need(len(h) >= 8 and len(boxes) < 10000, 'Invalid MP4 layout')
        size, t = struct.unpack_from('>I4s', h, 0)
        hl, zero = 8, False
        if size == 1:
            need(len(h) == 16, 'Truncated MP4')
            size, hl = u64(h, 8), 16
        elif size == 0:
            size, zero = fsize - at, True
        need(size >= hl and at + size <= fsize, 'Invalid top-level box size at byte %d' % at)
        boxes.append({'type': t, 'at': at, 'size': size, 'hl': hl, 'zero': zero})
        at += size
    return boxes


# ---------- header fields ----------

def hdr(box):
    d = box.data
    v = d[0]
    need(v in (0, 1), 'Unsupported %s version' % box.type.decode())
    w = 4 if v == 0 else 8
    if box.type == b'tkhd':
        return {'w': w, 'id': 12 if v == 0 else 20, 'dur': 20 if v == 0 else 28}
    return {'w': w, 'ts': 12 if v == 0 else 20, 'dur': 16 if v == 0 else 24}


def rd(d, off, w):
    return u32(d, off) if w == 4 else u64(d, off)


def wr(box, off, w, val):
    need(0 <= val < (0xFFFFFFFF if w == 4 else 1 << 63), 'Duration or ID out of range')
    d = bytearray(box.data)
    struct.pack_into('>I' if w == 4 else '>Q', d, off, val)
    box.data = bytes(d)


def duration_of(box):
    h = hdr(box)
    return rd(box.data, h['dur'], h['w'])


def timescale_of(box):
    return u32(box.data, hdr(box)['ts'])


def set_duration(box, val):
    h = hdr(box)
    wr(box, h['dur'], h['w'], val)


def track_id(trak):
    tk = trak.get(b'tkhd')
    return u32(tk.data, hdr(tk)['id'])


def handler(trak):
    h = trak.get(b'mdia').get(b'hdlr').data
    return h[8:12]


# ---------- AAC descriptors ----------

def read_desc(b, at, end):
    need(at < end, 'Missing AAC descriptor')
    tag = b[at]
    at += 1
    n = 0
    for _ in range(4):
        need(at < end, 'Truncated AAC descriptor')
        c = b[at]
        at += 1
        n = (n << 7) | (c & 0x7F)
        if not c & 0x80:
            break
    else:
        raise PatchError('Invalid AAC descriptor length')
    need(at + n <= end, 'AAC descriptor overflows')
    return tag, at, at + n


def write_desc(tag, payload):
    n = len(payload)
    return bytes([tag, 0x80 | (n >> 21) & 0x7F, 0x80 | (n >> 14) & 0x7F, 0x80 | (n >> 7) & 0x7F, n & 0x7F]) + payload


def asc_bits(asc):
    return ''.join(format(x, '08b') for x in asc)


def sl_sr_asc(freq_index):
    """AudioSpecificConfig: AAC-LC, channelConfiguration 0 + PCE with one side CPE."""
    t = format(freq_index, '04b')
    bits = ('00010' + t + '0000' + '000'           # AOT 2, rate, chanCfg 0, GASpecificConfig
            + '0000' + '01' + t                     # PCE: tag, object type LC, rate
            + '0000' + '0001' + '0000' + '00'       # front 0, side 1, back 0, lfe 0
            + '000' + '0000' + '000'                # assoc 0, cc 0, no mixdowns
            + '1' + '0000')                         # side element: CPE, tag 0
    bits += '0' * (-len(bits) % 8) + '00000000'    # byte align, comment_field_bytes 0
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


def patched_stsd(stsd):
    """Validate the single AAC-LC stereo entry; return (sample_rate, freq_index, new stsd body)."""
    d = stsd.data
    need(len(d) >= 16 and u32(d, 0) == 0 and u32(d, 4) == 1, 'Expected one audio sample description')
    entry = d[8:]
    need(u32(entry, 0) == len(entry) and entry[4:8] == b'mp4a', 'Audio must be AAC (mp4a)')
    body = entry[8:]
    need(len(body) >= 28 and u16(body, 8) == 0, 'Unsupported QuickTime audio description; re-export as MP4')
    need(u16(body, 16) == 2, 'Audio must be stereo')
    kids = parse_boxes(body[28:])
    esds = next((k for k in kids if k.type == b'esds'), None)
    need(esds is not None and not any(k.type in (b'wave', b'sinf') for k in kids),
         'Audio needs a direct AAC configuration (unencrypted, no QuickTime wrapper)')
    e = esds.data
    need(len(e) >= 4 and u32(e, 0) == 0, 'Unsupported esds version')
    tag, s, end = read_desc(e, 4, len(e))
    need(tag == 3 and end - s >= 3 and e[s + 2] & 0xE0 == 0, 'Unsupported ES descriptor flags')
    freq_index, parts, at = None, [], s + 3
    while at < end:
        t2, s2, e2 = read_desc(e, at, end)
        if t2 != 4:
            parts.append(e[at:e2])
        else:
            need(e2 - s2 >= 13 and e[s2] == 0x40, 'Audio is not MPEG-4 AAC')
            sub, at3 = [], s2 + 13
            while at3 < e2:
                t3, s3, e3 = read_desc(e, at3, e2)
                if t3 != 5:
                    sub.append(e[at3:e3])
                else:
                    need(freq_index is None, 'Duplicate AAC config')
                    bits = asc_bits(e[s3:e3])
                    need(len(bits) >= 16, 'AAC config too short')
                    need(int(bits[0:5], 2) == 2, 'Audio must be AAC-LC (not HE-AAC or other profiles)')
                    freq_index = int(bits[5:9], 2)
                    need(freq_index < len(SAMPLE_RATES), 'Unsupported AAC sample rate')
                    need(int(bits[9:13], 2) == 2, 'Audio must be plain stereo (channel config 2)')
                    need(bits[13:16] == '000', 'Unsupported AAC frame length or extension')
                    rest = bits[16:]
                    need('1' not in rest or re.fullmatch(r'01010110111001010*', rest),
                         'AAC extension data (SBR/PS) is not supported; export plain AAC-LC')
                    sub.append(write_desc(5, sl_sr_asc(freq_index)))
                at3 = e3
            parts.append(write_desc(4, e[s2:s2 + 13] + b''.join(sub)))
        at = e2
    need(freq_index is not None, 'Missing AAC config')
    new_esds = Box(b'esds', e[:4] + write_desc(3, e[s:s + 3] + b''.join(parts)))
    kids = [new_esds if k is esds else k for k in kids]
    entry2 = make_box(b'mp4a', body[:28] + b''.join(k.serialize() for k in kids))
    return SAMPLE_RATES[freq_index], freq_index, p32(0, 2) + entry + entry2


def describe_asc(asc):
    bits = asc_bits(asc)
    aot, fi, cc = int(bits[0:5], 2), int(bits[5:9], 2), int(bits[9:13], 2)
    rate = SAMPLE_RATES[fi] if fi < len(SAMPLE_RATES) else '?'
    s = 'AOT %d, %s Hz, channel config %d' % (aot, rate, cc)
    if cc == 0 and len(bits) >= 38:
        s += ' (PCE: front %d, side %d, back %d)' % (int(bits[26:30], 2), int(bits[30:34], 2), int(bits[34:38], 2))
    return s


def find_ascs(stsd_data):
    """AudioSpecificConfig bytes of every mp4a entry in an stsd body."""
    out = []
    try:
        for entry in parse_boxes(stsd_data[8:]):
            if entry.type != b'mp4a' or len(entry.data) < 28:
                continue
            for k in parse_boxes(entry.data[28:]):
                if k.type != b'esds':
                    continue
                e = k.data
                _, s, end = read_desc(e, 4, len(e))
                at = s + 3
                while at < end:
                    t2, s2, e2 = read_desc(e, at, end)
                    if t2 == 4:
                        at3 = s2 + 13
                        while at3 < e2:
                            t3, s3, e3 = read_desc(e, at3, e2)
                            if t3 == 5:
                                out.append(e[s3:e3])
                            at3 = e3
                    at = e2
    except (PatchError, struct.error, IndexError):
        pass
    return out


# ---------- sample table edits ----------

def table_count(box, entry_size):
    d = box.data
    need(len(d) >= 8 and u32(d, 0) & 0xFF000000 in (0, 0x01000000), 'Invalid %s table' % box.type.decode())
    n = u32(d, 4)
    need(len(d) == 8 + n * entry_size, 'Invalid %s count' % box.type.decode())
    return n


def extend_track(trak, *, tail_frames, rate, movie_ts, chunk_offsets, switch_desc, new_stsd):
    """Append the silent tail to one audio track. Returns the added duration in movie timescale."""
    mdia = trak.get(b'mdia')
    stbl = mdia.get(b'minf').get(b'stbl')
    for k in stbl.children:
        need(k.type in ALLOWED_AUDIO_STBL, 'Unsupported audio sample table box: ' + k.type.decode('latin1'))
    mdhd = mdia.get(b'mdhd')
    media_ts = timescale_of(mdhd)
    need(media_ts > 0, 'Invalid audio timescale')
    M = tail_frames

    # sample sizes
    stsz = stbl.get(b'stsz')
    d = stsz.data
    need(len(d) >= 12, 'Invalid stsz')
    const, count = u32(d, 4), u32(d, 8)
    need(count > 0 and len(d) == 12 + (0 if const else 4 * count), 'Invalid audio sample sizes')
    sizes = [const] * count if const else list(struct.unpack_from('>%dI' % count, d, 12))
    sizes += [len(SILENT_FRAME)] * M
    stsz.data = p32(0, 0, len(sizes)) + struct.pack('>%dI' % len(sizes), *sizes)

    # timing
    stts = stbl.get(b'stts')
    n = table_count(stts, 8)
    runs = [list(struct.unpack_from('>II', stts.data, 8 + 8 * i)) for i in range(n)]
    need(sum(r[0] for r in runs) == count, 'Audio timing does not match sample count')
    media_dur = sum(r[0] * r[1] for r in runs)
    tail_media = 0
    for t in range(M):
        delta = ((t + 1) * 1024 * media_ts) // rate - (t * 1024 * media_ts) // rate
        need(delta > 0, 'Audio timescale cannot represent AAC frames')
        tail_media += delta
        if runs and runs[-1][1] == delta:
            runs[-1][0] += 1
        else:
            runs.append([1, delta])
    stts.data = p32(0, len(runs)) + b''.join(p32(a, b) for a, b in runs)

    # chunks
    stco = stbl.find(b'stco')
    co64 = stbl.find(b'co64')
    need((stco is None) != (co64 is None), 'Expected exactly one chunk offset table')
    co = stco or co64
    w = 4 if co is stco else 8
    E = table_count(co, w)
    offsets = [rd(co.data, 8 + i * w, w) for i in range(E)] + chunk_offsets
    big = w == 8 or max(offsets) > 0xFFFFFFFF
    co.type = b'co64' if big else b'stco'
    co.data = p32(0, len(offsets)) + b''.join(struct.pack('>Q' if big else '>I', o) for o in offsets)

    stsc = stbl.get(b'stsc')
    sn = table_count(stsc, 12)
    need(sn > 0 and u32(stsc.data, 8) == 1, 'Invalid audio chunk map')
    stsc.data = (p32(0, sn + 2) + stsc.data[8:]
                 + p32(E + 1, M - SWITCH_FRAMES, 1) + p32(E + 2, SWITCH_FRAMES, switch_desc))

    # optional per-sample tables
    for k in stbl.children:
        r = k.data
        if k.type == b'ctts':
            cn = table_count(k, 8)
            k.data = p32(u32(r, 0), cn + 1) + r[8:] + p32(M, 0)
        elif k.type in (b'stss', b'stps'):
            cn = table_count(k, 4)
            k.data = p32(0, cn + M) + r[8:] + p32(*range(count + 1, count + M + 1))
        elif k.type == b'sdtp':
            need(len(r) == 4 + count, 'Invalid sdtp')
            k.data = r + bytes(M)
        elif k.type == b'sbgp':
            hl = 12 if r[0] == 1 else 8
            need(r[0] in (0, 1) and len(r) >= hl + 4, 'Invalid sbgp')
            gn = u32(r, hl)
            need(len(r) == hl + 4 + gn * 8, 'Invalid sbgp count')
            covered = sum(u32(r, hl + 4 + i * 8) for i in range(gn))
            need(covered <= count, 'sbgp exceeds sample count')
            add = count - covered + M
            if gn and u32(r, len(r) - 4) == 0:
                b = bytearray(r)
                struct.pack_into('>I', b, len(r) - 8, u32(r, len(r) - 8) + add)
                k.data = bytes(b)
            else:
                k.data = r[:hl] + p32(gn + 1) + r[hl + 4:] + p32(add, 0)

    if new_stsd is not None:
        stbl.get(b'stsd').data = new_stsd

    # durations
    old_mdhd = duration_of(mdhd)
    need(abs(old_mdhd - media_dur) <= (2048 * media_ts) // rate + 1, 'Audio duration header is inconsistent')
    set_duration(mdhd, media_dur + tail_media)
    tail_movie = max(1, round(tail_media * movie_ts / media_ts))

    edts = trak.find(b'edts')
    if edts is not None:
        elst = edts.get(b'elst')
        e = elst.data
        need(e[0] in (0, 1), 'Unsupported edit list')
        ew = 4 if e[0] == 0 else 8
        en = u32(e, 4)
        esz = 2 * ew + 4
        need(en in (1, 2) and len(e) == 8 + en * esz, 'Unsupported audio edit list')
        last = 8 + (en - 1) * esz
        seg = rd(e, last, ew)
        mt = struct.unpack_from('>i' if ew == 4 else '>q', e, last + ew)[0]
        need(mt >= 0 and u32(e, last + 2 * ew) == 0x10000, 'Unsupported audio edit')
        end_media = mt + seg * media_ts / movie_ts
        need(abs(end_media - media_dur) <= (2048 * media_ts) / rate + media_ts / movie_ts * 2,
             'Audio edit list trims the end of the audio; re-export without trimming')
        wr(elst, last, ew, seg + tail_movie)
    tkhd = trak.get(b'tkhd')
    set_duration(tkhd, duration_of(tkhd) + tail_movie)
    return tail_movie


def build(src, dst, variant, label, marker=True):
    tail, pce, dup = VARIANTS[variant]
    fsize = os.path.getsize(src)
    need(16 <= fsize, 'File too small')
    with open(src, 'rb') as f:
        top = scan_top(f, fsize)
        for b in top:
            need(b['type'] not in (b'moof', b'sidx', b'mfra', b'ssix'),
                 'Fragmented MP4 is not supported; re-export, or run: ffmpeg -i in.mp4 -c copy -movflags +faststart fixed.mp4')
        moovs = [b for b in top if b['type'] == b'moov']
        need(len(moovs) == 1 and any(b['type'] == b'mdat' for b in top), 'Expected one moov and at least one mdat')
        mv = moovs[0]
        need(mv['size'] <= 64 * 1024 * 1024, 'moov too large')
        f.seek(mv['at'])
        moov = parse_boxes(f.read(mv['size']))[0]
        for b in top:
            if b['type'] == b'free' and b['size'] < 4096:
                f.seek(b['at'] + b['hl'])
                if f.read(b['size'] - b['hl']).startswith(b'tikpatch/'):
                    raise PatchError('This file was already patched by tikpatch; use the original export')
            if b['type'] == b'free' and b['size'] > 16:
                f.seek(b['at'] + b['hl'] + 4)
                if f.read(4) == b'mvhd':
                    raise PatchError('This file already looks patched (an old moov is kept as free); use the original export')

    traks = [c for c in moov.children if c.type == b'trak']
    sounds = [t for t in traks if handler(t) == b'soun']
    need(len(sounds) == 1, 'Need exactly one audio track (found %d). Fix with: '
         'ffmpeg -i in.mp4 -map 0:v:0 -map 0:a:0 -c copy fixed.mp4' % len(sounds))
    need(any(handler(t) == b'vide' for t in traks), 'No video track found')
    audio = sounds[0]
    mvhd = moov.get(b'mvhd')
    movie_ts = timescale_of(mvhd)
    need(movie_ts > 0, 'Invalid movie timescale')

    stsd = audio.get(b'mdia').get(b'minf').get(b'stbl').get(b'stsd')
    rate, _, new_stsd = patched_stsd(stsd)   # validates the audio even for variants that skip PCE
    M = max(10, -(-35 * rate // (100 * 1024)))  # ceil(0.35 s worth of 1024-sample frames)

    tail_box = b''
    if tail:
        tail_box = make_box(b'mdat', SILENT_FRAME * M)
    data_off = fsize + 8
    chunk_offsets = [data_off, data_off + (M - SWITCH_FRAMES) * len(SILENT_FRAME)]

    clone = copy.deepcopy(audio) if dup else None
    if tail:
        extend_track(audio, tail_frames=M, rate=rate, movie_ts=movie_ts, chunk_offsets=chunk_offsets,
                     switch_desc=2 if pce else 1, new_stsd=new_stsd if pce else None)
        if clone is not None:
            extend_track(clone, tail_frames=M, rate=rate, movie_ts=movie_ts, chunk_offsets=chunk_offsets,
                         switch_desc=1, new_stsd=None)
    if clone is not None:
        ids = [track_id(t) for t in traks]
        need(all(ids) and len(set(ids)) == len(ids), 'Invalid track IDs')
        new_id = max(ids) + 1
        tk = clone.get(b'tkhd')
        wr(tk, hdr(tk)['id'], 4, new_id)
        moov.children.insert(moov.children.index(audio) + 1, clone)
        nh = hdr(mvhd)
        nto = 96 if nh['w'] == 4 else 108
        wr(mvhd, nto, 4, max(new_id + 1, u32(mvhd.data, nto) if u32(mvhd.data, nto) != 0xFFFFFFFF else 0))

    longest = max(duration_of(t.get(b'tkhd')) for t in moov.children if t.type == b'trak')
    if duration_of(mvhd) < longest:
        set_duration(mvhd, longest)

    marker_box = make_box(b'free', ('tikpatch/%s variant=%s label=%s' % (VERSION, variant, label or '-')).encode()) if marker else b''
    new_moov = moov.serialize()

    out_dir = os.path.dirname(os.path.abspath(dst))
    fd, tmp = tempfile.mkstemp(prefix='.tikpatch-', suffix='.mp4', dir=out_dir)
    try:
        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as f:
            shutil.copyfileobj(f, out, 8 * 1024 * 1024)
            out.seek(mv['at'] + 4)
            out.write(b'free')  # old index stays in place as padding, so every offset stays valid
            for b in top:
                if b['zero']:
                    need(b['size'] <= 0xFFFFFFFF, 'Size-zero box too large to rewrite')
                    out.seek(b['at'])
                    out.write(struct.pack('>I', b['size']))
            out.seek(0, 2)
            out.write(tail_box + marker_box + new_moov)
        if os.path.exists(dst):
            raise PatchError('Output exists, refusing to overwrite: ' + dst)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {'tail_frames': M if tail else 0, 'rate': rate, 'pce': pce, 'dup': dup}


# ---------- inspect ----------

SIGNATURES = [b'x264 - core', b'x265 (build', b'Lavc', b'Lavf', b'NVIDIA', b'H.264 NVIDIA', b'AMD', b'Intel',
              b'Apple', b'HandBrake', b'Adobe', b'DaVinci', b'Premiere', b'CapCut', b'bytevc', b'TikQuick',
              b'Compressbase', b'tikpatch', b'vid:', b'TikTok']


def strings_with(buf, sigs):
    found = []
    for m in re.finditer(rb'[\x20-\x7e]{6,}', buf):
        s = m.group()
        if any(sig.lower() in s.lower() for sig in sigs):
            found.append(s.decode('ascii')[:160])
    return found


def inspect(path):
    fsize = os.path.getsize(path)
    print('=' * 72)
    print(path)
    print('size %.2f MB' % (fsize / 1e6))
    with open(path, 'rb') as f:
        head = f.read(4)
        f.seek(0)
        if head[:1] == b'\x47':
            print('MPEG-TS stream (not an MP4 container). Container analysis skipped.')
            return
        top = scan_top(f, fsize)
        print('layout: ' + '  '.join('%s(%s)' % (b['type'].decode('latin1'), human(b['size'])) for b in top))
        moov_box = next(b for b in top if b['type'] == b'moov')
        f.seek(moov_box['at'])
        moov = parse_boxes(f.read(moov_box['size']))[0]
        frees = []
        for b in top:
            if b['type'] in (b'free', b'skip') and b['size'] > 8:
                f.seek(b['at'] + b['hl'])
                frees.append(f.read(min(b['size'] - b['hl'], 300)))
        mdat = next((b for b in top if b['type'] == b'mdat'), None)
        sei = b''
        if mdat:
            f.seek(mdat['at'] + mdat['hl'])
            sei = f.read(min(mdat['size'], 1 << 20))
    mvhd = moov.get(b'mvhd')
    dur = duration_of(mvhd) / timescale_of(mvhd)
    if duration_of(mvhd) in (0xFFFFFFFF, (1 << 64) - 1):  # "unknown" per spec: use the longest track
        dur = max(duration_of(m) / timescale_of(m) for m in
                  (t.get(b'mdia').get(b'mdhd') for t in moov.children if t.type == b'trak'))
    print('duration %.3f s, overall %.2f Mbps' % (dur, fsize * 8 / dur / 1e6 if dur else 0))
    marker = next((x for x in frees if x.startswith(b'tikpatch/')), None)
    old_moov_as_free = any(x[4:8] == b'mvhd' for x in frees)
    tags = strings_with(moov.serialize(), SIGNATURES)
    enc_strings = strings_with(sei, SIGNATURES)
    sounds, slsr = 0, False
    for t in (c for c in moov.children if c.type == b'trak'):
        h = handler(t).decode('latin1')
        mdhd = t.get(b'mdia').get(b'mdhd')
        ts = timescale_of(mdhd)
        tdur = duration_of(mdhd) / ts if ts else 0
        stbl = t.get(b'mdia').get(b'minf').get(b'stbl')
        stsd = stbl.get(b'stsd').data
        nent = u32(stsd, 4)
        fourcc = stsd[12:16].decode('latin1')
        stsz = stbl.find(b'stsz')
        nsamp, total = 0, 0
        if stsz:
            const, nsamp = u32(stsz.data, 4), u32(stsz.data, 8)
            total = const * nsamp if const else sum(struct.unpack_from('>%dI' % nsamp, stsz.data, 12))
        tk = t.get(b'tkhd')
        tid = u32(tk.data, hdr(tk)['id'])
        line = 'track %d %s %s' % (tid, h, fourcc)
        if h == 'vide':
            wd = u16(stsd, 8 + 8 + 24)
            ht = u16(stsd, 8 + 8 + 26)
            line += ' %dx%d, %d frames, %.2f fps, %.2f Mbps' % (wd, ht, nsamp, nsamp / tdur if tdur else 0,
                                                              total * 8 / tdur / 1e6 if tdur else 0)
        elif h == 'soun':
            sounds += 1
            line += ' %d frames, %.0f kbps, %d sample description(s)' % (nsamp, total * 8 / tdur / 1e3 if tdur else 0, nent)
        print(line)
        if h == 'soun':
            for asc in find_ascs(stsd):
                desc = describe_asc(asc)
                print('    AAC config: ' + desc)
                if 'side 1' in desc:
                    slsr = True
    print('metadata tags: ' + ('; '.join(sorted(set(tags))) or '-'))
    print('encoder strings in first video data: ' + ('; '.join(sorted(set(enc_strings))[:6]) or '-'))
    last_types = [b['type'] for b in top[-3:]]
    print('fingerprints:')
    print('    moov at end:            %s' % ('yes' if top[-1]['type'] == b'moov' else 'no'))
    print('    old moov kept as free:  %s' % ('yes' if old_moov_as_free else 'no'))
    print('    tiny tail mdat:         %s' % ('yes' if any(b['type'] == b'mdat' and b['size'] < 4096 for b in top) else 'no'))
    print('    SL/SR side-channel desc:%s' % (' yes' if slsr else ' no'))
    print('    audio tracks:           %d' % sounds)
    print('    tikpatch marker:        %s' % (marker.decode('ascii', 'replace') if marker else 'no'))
    patched = slsr or marker or old_moov_as_free
    if patched:
        print('verdict: container tricks are intact. If this file was downloaded from TikTok, '
              'TikTok served the uploaded file without re-encoding it (passthrough).')
    else:
        print('verdict: no patch fingerprints. For a definitive answer on a TikTok download, run: '
              'tikpatch compare YOUR_EXPORT.mp4 DOWNLOAD.mp4')


def load_moov(f, fsize):
    top = scan_top(f, fsize)
    mv = [b for b in top if b['type'] == b'moov']
    need(len(mv) == 1, 'Expected one moov')
    f.seek(mv[0]['at'])
    return parse_boxes(f.read(mv[0]['size']))[0]


def video_summary(path, n=90):
    """Resolution, fps, bitrate and SHA-1 of the first n video samples (exact bytes)."""
    import hashlib
    with open(path, 'rb') as f:
        need(f.read(1) != b'\x47', 'MPEG-TS files are not supported by compare')
        moov = load_moov(f, os.path.getsize(path))
        trak = next((t for t in moov.children if t.type == b'trak' and handler(t) == b'vide'), None)
        need(trak is not None, 'No video track in ' + path)
        mdhd = trak.get(b'mdia').get(b'mdhd')
        stbl = trak.get(b'mdia').get(b'minf').get(b'stbl')
        stsz = stbl.get(b'stsz').data
        const, count = u32(stsz, 4), u32(stsz, 8)
        sizes = [const] * count if const else list(struct.unpack_from('>%dI' % count, stsz, 12))
        co = stbl.find(b'stco') or stbl.get(b'co64')
        w = 4 if co.type == b'stco' else 8
        offs = [rd(co.data, 8 + i * w, w) for i in range(u32(co.data, 4))]
        sc = stbl.get(b'stsc').data
        runs = [struct.unpack_from('>III', sc, 8 + 12 * i) for i in range(u32(sc, 4))]
        hashes, sample = [], 0
        for ci, off in enumerate(offs):
            chunk = ci + 1
            spc = next(r[1] for r in reversed(runs) if r[0] <= chunk)
            for _ in range(spc):
                if sample >= min(n, count):
                    break
                f.seek(off)
                hashes.append(hashlib.sha1(f.read(sizes[sample])).hexdigest())
                off += sizes[sample]
                sample += 1
            if sample >= min(n, count):
                break
        stsd = stbl.get(b'stsd').data
        dur = duration_of(mdhd) / timescale_of(mdhd)
        return {'codec': stsd[12:16].decode('latin1'), 'w': u16(stsd, 40), 'h': u16(stsd, 42), 'frames': count,
                'fps': count / dur if dur else 0, 'mbps': sum(sizes) * 8 / dur / 1e6 if dur else 0, 'hashes': hashes}


def compare(original, download):
    a, b = video_summary(original), video_summary(download)
    for name, v in (('export  ', a), ('download', b)):
        print('%s %s %dx%d, %d frames, %.2f fps, %.2f Mbps' % (name, v['codec'], v['w'], v['h'], v['frames'], v['fps'], v['mbps']))
    same = sum(1 for x in b['hashes'] if x in set(a['hashes']))
    total = len(b['hashes'])
    print('identical video frames (first %d checked): %d' % (total, same))
    if total and same == total:
        print('RESULT: PASSTHROUGH. TikTok served your exact video stream, zero quality loss.')
    elif same:
        print('RESULT: PARTIAL MATCH (%d/%d). Unusual; possibly trimmed or remuxed.' % (same, total))
    else:
        print('RESULT: RE-ENCODED. TikTok made its own encode (%.1f%% of your bitrate).'
              % (100 * b['mbps'] / a['mbps'] if a['mbps'] else 0))


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d%s' if unit == 'B' else '%.1f%s') % (n, unit)
        n /= 1024


# ---------- CLI ----------

def default_out(src, variant):
    base, _ = os.path.splitext(src)
    return '%s_%s.mp4' % (base, 'tikpatch' if variant == 'full' else variant)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='tikpatch', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('patch', help='patch one video')
    p.add_argument('input')
    p.add_argument('output', nargs='?')
    p.add_argument('--variant', choices=list(VARIANTS), default='tail-pce',
                   help='; '.join('%s: %s' % kv for kv in VARIANT_HELP.items()))
    p.add_argument('--label', help='text stored in a marker box, handy to recognise test uploads later')
    p.add_argument('--no-marker', action='store_true', help='do not write the tikpatch marker box')
    m = sub.add_parser('matrix', help='write all test variants plus a control. Give one clip, or several '
                                      'different clips (recommended: one clip per test, avoids TikTok duplicate detection)')
    m.add_argument('inputs', nargs='+')
    m.add_argument('--outdir')
    i = sub.add_parser('inspect', help='analyse MP4 files')
    i.add_argument('files', nargs='+')
    c = sub.add_parser('compare', help='did TikTok pass your video through untouched? (export vs download)')
    c.add_argument('export')
    c.add_argument('download')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'patch':
            src = os.path.abspath(os.path.expanduser(a.input))
            dst = os.path.abspath(os.path.expanduser(a.output or default_out(src, a.variant)))
            need(src != dst, 'Output must differ from input')
            need(not os.path.exists(dst), 'Output exists, refusing to overwrite: ' + dst)
            info = build(src, dst, a.variant, a.label, marker=not a.no_marker)
            print('Saved %s (variant %s, %d silent frames at %d Hz)' % (dst, a.variant, info['tail_frames'], info['rate']))
        elif a.cmd == 'matrix':
            srcs = [os.path.abspath(os.path.expanduser(x)) for x in a.inputs]
            plan = [('control', None, True)] + [(v, v, True) for v in VARIANTS] + [('full-nomarker', 'full', False)]
            stem = os.path.splitext(os.path.basename(srcs[0]))[0]
            outdir = os.path.abspath(os.path.expanduser(a.outdir or os.path.join(os.path.dirname(srcs[0]), stem + '_tests')))
            os.makedirs(outdir, exist_ok=True)
            need(not any(n.endswith('.mp4') for n in os.listdir(outdir)), 'Test folder already has videos: ' + outdir)
            if len(srcs) > 1:
                need(len(srcs) <= len(plan), 'At most %d clips (one per test)' % len(plan))
                plan = plan[:len(srcs)]
            print('%-28s %-26s %s' % ('file', 'source clip', 'what it tests'))
            for n, (name, variant, marker) in enumerate(plan):
                src = srcs[n] if len(srcs) > 1 else srcs[0]
                out = os.path.join(outdir, '%02d_%s.mp4' % (n, name))
                if variant is None:
                    shutil.copyfile(src, out)
                    what = 'untouched original (control)'
                else:
                    build(src, out, variant, '%02d-%s' % (n, name), marker=marker)
                    what = VARIANT_HELP[variant] if marker else 'same as full, without the tikpatch marker box'
                print('%-28s %-26s %s' % (os.path.basename(out), os.path.basename(src)[:26], what))
            print('Written to ' + outdir)
        elif a.cmd == 'compare':
            compare(os.path.expanduser(a.export), os.path.expanduser(a.download))
        else:
            for path in a.files:
                inspect(os.path.expanduser(path))
    except PatchError as e:
        sys.exit('tikpatch: ' + str(e))


if __name__ == '__main__':
    main()
