#!/usr/bin/env python3
"""Gather Titan M / Titan M2 (GSC) firmware from Pixel factory images without
downloading the images (Python 3.9+, standard library only).

Google lists every factory image at https://developers.google.com/android/images
as a dl.google.com ZIP link next to its SHA-256, and dl.google.com honours
HTTP byte ranges, so the ZIP directory and single members can be read in place
the way `partialzip` does. This tool has three steps:

  index   read the listing page once and write a JSON index of every factory
          image URL, keyed by product codename
  list    print the members of one factory ZIP (and of the image-*.zip inside
          it when that is stored uncompressed), reading a few hundred KB
  listings
          do that for every image in the index and save each as
          <out>/<codename>/<build>.json, the complete file list per release
  report  group the gathered blobs by chip, flavor and SHA-256 and decode
          each distinct one (Titan SignedHeader versions, build tags, FBPK
          platform and pack version) into report.json
  fetch   walk the index and pull every GSC firmware file (`*.rec`, `*.bin`:
          citadel ec.bin / ec.rec on Pixel 3-5, dauntless d2/d3/d3m2.bin on
          Tensor) out of each image into a folder, with a manifest of SHA-256s

Reading the page or an image means accepting Google's factory-image terms, the
same as clicking Acknowledge on the page. Nothing here touches a phone.

`partialzip` (github.com/marcograss/partialzip) is used for the ZIP reads when
it is on PATH or named with --partialzip; otherwise the same byte-range reads
are done here with urllib, so no dependency is needed.
"""
import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

IMAGES_PAGE = "https://developers.google.com/android/images"
LINK_PREFIX = "https://dl.google.com/dl/android/aosp/"
# The cookie the page's Acknowledge button sets; without it there are no links.
TOS_ACK_COOKIE = "devsite_wall_acks=nexus-image-tos"
USER_AGENT = "pixel-restore-gsc-fetch/0.1"

# Codename -> (marketing name, GSC, SoC, platform, release year, family).
# Pixel 3 through 5 carry a Titan M (Citadel, firmware ec.bin / rescue
# ec.rec); Tensor Pixels a Titan M2 (Dauntless, firmware <flavor>.ec.bin).
# Pixel 10a and Pixel 11 entries are inferred from the family, not confirmed.
# Unlisted codenames on the page are indexed too, with gsc "unknown".
DEVICE_ROWS = [
    ("sailfish", "Pixel", None, "Snapdragon 821", "msm8996", 2016, "Pixel"),
    ("marlin", "Pixel XL", None, "Snapdragon 821", "msm8996", 2016, "Pixel"),
    ("walleye", "Pixel 2", None, "Snapdragon 835", "msm8998", 2017, "Pixel 2"),
    ("taimen", "Pixel 2 XL", None, "Snapdragon 835", "msm8998", 2017, "Pixel 2"),
    ("blueline", "Pixel 3", "citadel", "Snapdragon 845", "sdm845", 2018, "Pixel 3"),
    ("crosshatch", "Pixel 3 XL", "citadel", "Snapdragon 845", "sdm845", 2018, "Pixel 3"),
    ("sargo", "Pixel 3a", "citadel", "Snapdragon 670", "sdm670", 2019, "Pixel 3a"),
    ("bonito", "Pixel 3a XL", "citadel", "Snapdragon 670", "sdm670", 2019, "Pixel 3a"),
    ("flame", "Pixel 4", "citadel", "Snapdragon 855", "sm8150", 2019, "Pixel 4"),
    ("coral", "Pixel 4 XL", "citadel", "Snapdragon 855", "sm8150", 2019, "Pixel 4"),
    ("sunfish", "Pixel 4a", "citadel", "Snapdragon 730G", "sm7150", 2020, "Pixel 4a"),
    ("bramble", "Pixel 4a 5G", "citadel", "Snapdragon 765G", "sm7250", 2020, "Pixel 4a 5G"),
    ("redfin", "Pixel 5", "citadel", "Snapdragon 765G", "sm7250", 2020, "Pixel 5"),
    ("barbet", "Pixel 5a", "citadel", "Snapdragon 765G", "sm7250", 2021, "Pixel 5a"),
    ("oriole", "Pixel 6", "dauntless", "Tensor G1", "gs101", 2021, "Pixel 6"),
    ("raven", "Pixel 6 Pro", "dauntless", "Tensor G1", "gs101", 2021, "Pixel 6"),
    ("bluejay", "Pixel 6a", "dauntless", "Tensor G1", "gs101", 2022, "Pixel 6"),
    ("panther", "Pixel 7", "dauntless", "Tensor G2", "gs201", 2022, "Pixel 7"),
    ("cheetah", "Pixel 7 Pro", "dauntless", "Tensor G2", "gs201", 2022, "Pixel 7"),
    ("lynx", "Pixel 7a", "dauntless", "Tensor G2", "gs201", 2023, "Pixel 7"),
    ("felix", "Pixel Fold", "dauntless", "Tensor G2", "gs201", 2023, "Pixel 7"),
    ("tangorpro", "Pixel Tablet", "dauntless", "Tensor G2", "gs201", 2023, "Pixel 7"),
    ("shiba", "Pixel 8", "dauntless", "Tensor G3", "zuma", 2023, "Pixel 8"),
    ("husky", "Pixel 8 Pro", "dauntless", "Tensor G3", "zuma", 2023, "Pixel 8"),
    ("akita", "Pixel 8a", "dauntless", "Tensor G3", "zuma", 2024, "Pixel 8"),
    ("tokay", "Pixel 9", "dauntless", "Tensor G4", "zumapro", 2024, "Pixel 9"),
    ("caiman", "Pixel 9 Pro", "dauntless", "Tensor G4", "zumapro", 2024, "Pixel 9"),
    ("komodo", "Pixel 9 Pro XL", "dauntless", "Tensor G4", "zumapro", 2024, "Pixel 9"),
    ("comet", "Pixel 9 Pro Fold", "dauntless", "Tensor G4", "zumapro", 2024, "Pixel 9"),
    ("tegu", "Pixel 9a", "dauntless", "Tensor G4", "zumapro", 2025, "Pixel 9"),
    ("frankel", "Pixel 10", "dauntless", "Tensor G5", "laguna", 2025, "Pixel 10"),
    ("blazer", "Pixel 10 Pro", "dauntless", "Tensor G5", "laguna", 2025, "Pixel 10"),
    ("mustang", "Pixel 10 Pro XL", "dauntless", "Tensor G5", "laguna", 2025, "Pixel 10"),
    ("rango", "Pixel 10 Pro Fold", "dauntless", "Tensor G5", "laguna", 2025, "Pixel 10"),
    ("stallion", "Pixel 10a", "dauntless", "Tensor G5", "laguna", 2026, "Pixel 10"),
    ("cubs", "Pixel 11", "dauntless", "Tensor G6", None, 2026, "Pixel 11"),
    ("grizzly", "Pixel 11 Pro", "dauntless", "Tensor G6", None, 2026, "Pixel 11"),
    ("kodiak", "Pixel 11 Pro XL", "dauntless", "Tensor G6", None, 2026, "Pixel 11"),
    ("yogi", "Pixel 11 Pro Fold", "dauntless", "Tensor G6", None, 2026, "Pixel 11"),
]
INFERRED = {"stallion", "cubs", "grizzly", "kodiak", "yogi"}
DEVICES = {r[0]: (r[1], r[2]) for r in DEVICE_ROWS}
# Known GSC firmware flavors (the `gsc-flavor` fastboot variable) per device.
GSC_FLAVOR = {"komodo": "d3m2"}
CARRIERS = ("verizon", "t-mobile", "at&t", "telstra", "softbank", "kddi", "docomo",
            "emea", "jp", "tw", "sprint", "google fi", "rogers", "optus", "vodafone")
# GSC firmware file names inside an image: anything .rec, and .bin files named
# for a chip (ec.bin, d2.bin, d3.bin, d3m2.bin, ...), or anything under a
# citadel/dauntless/gsc directory.
GSC_NAME = re.compile(r"(^|/)((ec|d\d[a-z0-9]*|citadel|dauntless|gsc)(\.ec)?|[^/]+\.ec)\.(bin|rec)$", re.I)
GSC_DIR = re.compile(r"(^|/)(citadel|dauntless|gsc|titan)[^/]*/", re.I)


def fail(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------- listing page

def strip_tags(s):
    return " ".join(re.sub(r"<[^>]*>", " ", s).split())


def parse_page(html):
    """{codename: {"name", "gsc", "images": [...]}} from the listing HTML.

    Every factory image is a `LINK_PREFIX<codename>-<build>-factory-<id>.zip`
    link; its row's first cell is the description and the SHA-256 is the
    first 64-hex run after the link. The page's `<h2 id="<codename>">`
    heading gives the marketing name for codenames this file does not know."""
    names = {}
    for m in re.finditer(r'<h2[^>]*id="([a-z0-9]+)"[^>]*>(.*?)</h2>', html, re.I | re.S):
        text = strip_tags(m.group(2))
        q = re.search(r'"[^"]+"\s+for\s+(.+)$', text)
        names[m.group(1).lower()] = q.group(1).strip() if q else text
    index = {}
    seen = set()
    for m in re.finditer(re.escape(LINK_PREFIX) + r'([a-z0-9]+)-([^"\s<>]+?)-factory-([0-9a-f]+)\.zip', html, re.I):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        codename, build = m.group(1).lower(), m.group(2).lower()
        window = html[m.end():m.end() + 4000]
        sha = next((t.lower() for t in re.split(r"[^0-9a-fA-F]", window) if len(t) == 64), None)
        row_start = html.rfind("<tr", 0, m.start())
        link_start = html.rfind("<a", 0, m.start())
        desc = strip_tags(html[row_start:link_start]) if 0 <= row_start < link_start else ""
        desc = re.sub(r"\s*(Link|Flash)\s*$", "", desc).strip()
        desc = re.sub(r"\s*Flash$", "", desc).strip()
        low = desc.lower()
        carrier = any(f"({c}" in low or f", {c}" in low for c in CARRIERS)
        entry = index.setdefault(codename, {
            "name": DEVICES.get(codename, (names.get(codename, codename), None))[0],
            "gsc": DEVICES[codename][1] if codename in DEVICES else "unknown",
            "images": [],
        })
        entry["images"].append({
            "build": build, "url": url, "sha256": sha, "description": desc,
            "carrier": carrier, "file": url.rsplit("/", 1)[1],
        })
    for entry in index.values():
        entry["images"].sort(key=lambda i: release_key(i["description"], i["build"]))
    return index


MONTHS = {m: n for n, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def release_key(description, build):
    """Newest-last order: the "Mon YYYY" in the row's description (the
    release date; build ids are not in release order), then the build id's
    YYMMDD and number, e.g. cp3a.260905.009."""
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{4})\b", description.lower())
    when = (int(m.group(2)), MONTHS[m.group(1)]) if m else (0, 0)
    parts = build.split(".")
    date = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    num = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return (when, date, num, build)


def http_get(url, headers=None, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def listing_html(page_file=None):
    if page_file:
        return Path(page_file).read_text(errors="replace")
    log(f"reading {IMAGES_PAGE} (this accepts Google's factory image terms) ...")
    with http_get(IMAGES_PAGE, {"Cookie": TOS_ACK_COOKIE}) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------------------------------------ byte-range reads

class RangeSource:
    """Byte ranges of a fixed-length blob."""
    len = 0
    bytes_read = 0

    def read(self, start, end):
        raise NotImplementedError


class HttpRange(RangeSource):
    def __init__(self, url, retries=4):
        self.url, self.retries = url, retries
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                fail(f"{url}: HTTP {r.status}")
            length = r.headers.get("Content-Length")
            if not length:
                fail(f"{url}: server did not report the file size")
            self.len = int(length)
            ranges = r.headers.get("Accept-Ranges", "bytes")
            if "bytes" not in ranges:
                fail(f"{url}: server refuses byte-range requests")

    def read(self, start, end):
        if start >= end:
            return b""
        delay = 2
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(self.url, headers={
                    "User-Agent": USER_AGENT, "Range": f"bytes={start}-{end - 1}"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    if r.status != 206:
                        fail(f"{self.url}: server ignored the Range header (HTTP {r.status})")
                    data = r.read(end - start)
                if len(data) != end - start:
                    raise IOError(f"short range read: {len(data)} of {end - start}")
                self.bytes_read += len(data)
                return data
            except (urllib.error.URLError, IOError, ConnectionError) as e:
                if attempt == self.retries:
                    raise
                log(f"  range {start}-{end} failed ({e}); retrying in {delay}s")
                time.sleep(delay)
                delay *= 2


class FileRange(RangeSource):
    def __init__(self, path):
        self.f = open(path, "rb")
        self.len = os.fstat(self.f.fileno()).st_size

    def read(self, start, end):
        self.f.seek(start)
        data = self.f.read(end - start)
        self.bytes_read += len(data)
        return data


class SubRange(RangeSource):
    """A stored (uncompressed) ZIP member seen as its own range source, so a
    nested ZIP can be read in place."""
    def __init__(self, parent, offset, length):
        self.parent, self.offset, self.len = parent, offset, length

    def read(self, start, end):
        return self.parent.read(self.offset + start, self.offset + end)

    @property
    def bytes_read(self):
        return self.parent.bytes_read


# ------------------------------------------------------------------ zip layer

EOCD_SIG = 0x06054b50
EOCD64_LOC_SIG = 0x07064b50
EOCD64_SIG = 0x06064b50
CENTRAL_SIG = 0x02014b50
LOCAL_SIG = 0x04034b50


class Member:
    __slots__ = ("name", "method", "crc32", "csize", "usize", "offset", "data_offset")

    def __init__(self, name, method, crc32, csize, usize, offset):
        self.name, self.method, self.crc32 = name, method, crc32
        self.csize, self.usize, self.offset = csize, usize, offset
        self.data_offset = None

    @property
    def base(self):
        return self.name.rsplit("/", 1)[-1]

    def as_dict(self):
        return {"name": self.name, "method": self.method, "compressed_size": self.csize,
                "size": self.usize, "crc32": f"{self.crc32:08x}"}


def zip_members(src):
    """Central directory of a (possibly ZIP64) archive from its tail only."""
    tail_len = min(src.len, 65536 + 22 + 20)
    tail = src.read(src.len - tail_len, src.len)
    pos = tail.rfind(struct.pack("<I", EOCD_SIG))
    if pos < 0:
        raise ValueError("not a ZIP file (no end-of-central-directory record)")
    _, _, _, _, count, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[pos:pos + 22])
    if 0xFFFF in (count,) or 0xFFFFFFFF in (cd_size, cd_off):
        loc = tail.rfind(struct.pack("<I", EOCD64_LOC_SIG), 0, pos)
        if loc < 0:
            raise ValueError("ZIP64 archive without a ZIP64 locator")
        eocd64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        rec = src.read(eocd64_off, eocd64_off + 56)
        if struct.unpack("<I", rec[:4])[0] != EOCD64_SIG:
            raise ValueError("bad ZIP64 end-of-central-directory record")
        count, cd_size, cd_off = struct.unpack("<QQQ", rec[32:56])
    cd = src.read(cd_off, cd_off + cd_size)
    members, p = [], 0
    for _ in range(count):
        if struct.unpack("<I", cd[p:p + 4])[0] != CENTRAL_SIG:
            raise ValueError("bad central directory entry")
        method = struct.unpack("<H", cd[p + 10:p + 12])[0]
        crc, csize, usize, nlen, xlen, clen = struct.unpack("<IIIHHH", cd[p + 16:p + 34])
        off = struct.unpack("<I", cd[p + 42:p + 46])[0]
        name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        extra = cd[p + 46 + nlen:p + 46 + nlen + xlen]
        q = 0
        while q + 4 <= len(extra):
            tag, size = struct.unpack("<HH", extra[q:q + 4])
            if tag == 1:
                body, r = extra[q + 4:q + 4 + size], 0
                if usize == 0xFFFFFFFF:
                    usize = struct.unpack("<Q", body[r:r + 8])[0]; r += 8
                if csize == 0xFFFFFFFF:
                    csize = struct.unpack("<Q", body[r:r + 8])[0]; r += 8
                if off == 0xFFFFFFFF:
                    off = struct.unpack("<Q", body[r:r + 8])[0]; r += 8
            q += 4 + size
        members.append(Member(name, method, crc, csize, usize, off))
        p += 46 + nlen + xlen + clen
    return members


def member_data_offset(src, m):
    """Where a member's bytes start: after its local header."""
    if m.data_offset is None:
        head = src.read(m.offset, m.offset + 30)
        if struct.unpack("<I", head[:4])[0] != LOCAL_SIG:
            raise ValueError(f"{m.name}: bad local file header")
        nlen, xlen = struct.unpack("<HH", head[26:30])
        m.data_offset = m.offset + 30 + nlen + xlen
    return m.data_offset


def extract(src, m, sink=None):
    """A member's bytes (or write them to `sink` and return the CRC)."""
    start = member_data_offset(src, m)
    if m.method not in (0, 8):
        raise ValueError(f"{m.name}: unsupported compression method {m.method}")
    dec = zlib.decompressobj(-15) if m.method == 8 else None
    crc, out, pos, chunk = 0, io.BytesIO() if sink is None else sink, start, 8 << 20
    while pos < start + m.csize:
        raw = src.read(pos, min(pos + chunk, start + m.csize))
        pos += len(raw)
        piece = dec.decompress(raw) if dec else raw
        crc = zlib.crc32(piece, crc)
        out.write(piece)
    if dec:
        piece = dec.flush()
        crc = zlib.crc32(piece, crc)
        out.write(piece)
    if crc != m.crc32:
        raise ValueError(f"{m.name}: CRC mismatch ({crc:08x} != {m.crc32:08x})")
    return out.getvalue() if sink is None else crc


# ------------------------------------------------------------ partialzip CLI

def partialzip_bin(explicit=None):
    if explicit:
        return explicit
    return shutil.which("partialzip")


def partialzip_list(binary, url):
    out = subprocess.run([binary, "list", url], capture_output=True, text=True)
    if out.returncode:
        raise IOError(out.stderr.strip() or f"partialzip list failed on {url}")
    names = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            names.append(line.split()[0] if " " in line else line)
    return names


def partialzip_download(binary, url, name, dest):
    out = subprocess.run([binary, "download", url, name, str(dest)], capture_output=True, text=True)
    if out.returncode:
        raise IOError(out.stderr.strip() or f"partialzip download failed for {name}")


# ---------------------------------------------------------------- the walk

def is_gsc(name):
    return bool(GSC_NAME.search(name) or (GSC_DIR.search(name) and name.lower().endswith((".bin", ".rec"))))


def fbpk_entries(data):
    """(name, bytes) for every partition-data entry in an FBPK v2 image
    (the `bootloader-*.img` / `radio-*.img` container): a 112-byte header
    with the entry count at 104, then 104-byte entries (kind u32, name[36],
    product[40], offset u64, size u64, slotted u32, crc32 u32)."""
    if len(data) < 112 or data[:4] != b"FBPK" or struct.unpack("<I", data[4:8])[0] != 2:
        return []
    header_size, entry_size = struct.unpack("<II", data[8:16])
    count = struct.unpack("<I", data[104:108])[0]
    out = []
    for i in range(count):
        base = header_size + i * entry_size
        if base + entry_size > len(data):
            break
        kind = struct.unpack("<I", data[base:base + 4])[0]
        name = data[base + 4:base + 40].split(b"\0", 1)[0].decode("ascii", "replace")
        offset, size = struct.unpack("<QQ", data[base + 80:base + 96])
        if kind == 1 and 0 < size and offset + size <= len(data):
            out.append((name, data[offset:offset + size]))
    return out


class Hit:
    def __init__(self, where, data, kind, member=None):
        self.where, self.data, self.kind = where, data, kind
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.size = len(data)


KINDS = ("gsc", "bin", "bootloader", "radio")


def kind_of(name, kinds):
    """Which wanted kind a ZIP member is, or None. `gsc` is the Titan
    firmware (d3m2.ec.bin, ec.rec, ...), `bin` any other .bin or .rec,
    `bootloader` / `radio` the FBPK packs at the top of a factory ZIP."""
    base = name.rsplit("/", 1)[-1].lower()
    if "gsc" in kinds and is_gsc(name):
        return "gsc"
    if "bin" in kinds and base.endswith((".bin", ".rec")):
        return "bin"
    if "bootloader" in kinds and base.startswith("bootloader") and base.endswith(".img"):
        return "bootloader"
    if "radio" in kinds and base.startswith("radio") and base.endswith(".img"):
        return "radio"
    return None


def walk_zip(src, label, stream_nested, max_size, log_members=False, kinds=("gsc",), seen=None):
    """Every wanted file reachable in a factory ZIP: top-level members of the
    wanted kinds, the same inside `image-*.zip` (read in place when stored,
    streamed through when deflated and `stream_nested`), and GSC-named
    partitions inside FBPK `bootloader-*.img` / `radio-*.img`."""
    hits, notes = [], []
    members = zip_members(src)
    if log_members:
        for m in members:
            log(f"  {m.usize:>12}  {'stored' if m.method == 0 else 'deflate'}  {m.name}")
    for m in members:
        if m.name.endswith("/"):
            continue
        kind = kind_of(m.name, kinds)
        if kind in ("gsc", "bin") and m.usize > max_size:
            notes.append(f"{m.name}: {m.usize} bytes is over --max-size, skipped")
            kind = None
        if kind:
            data = extract(src, m)
            hits.append(Hit(f"{label}:{m.name}", data, kind, m))
            if kind in ("bootloader", "radio") and "gsc" in kinds:
                for name, body in fbpk_entries(data):
                    if GSC_NAME.search(name + ".bin"):
                        hits.append(Hit(f"{label}:{m.base}:{name}", body, "gsc"))
        elif "gsc" in kinds and is_fbpk(m.name) and m.usize <= 256 << 20:
            for name, body in fbpk_entries(extract(src, m)):
                if GSC_NAME.search(name + ".bin"):
                    hits.append(Hit(f"{label}:{m.base}:{name}", body, "gsc"))
        if m.base.lower().endswith(".zip"):
            if m.method == 0:
                inner = SubRange(src, member_data_offset(src, m), m.usize)
                h, n = walk_zip(inner, f"{label}:{m.base}", stream_nested, max_size, log_members, kinds, seen)
                hits += h
                notes += n
            elif stream_nested:
                log(f"  {m.base} is deflated inside {label}; streaming {m.csize / 1e6:.0f} MB through ...")
                tmp = io.BytesIO()
                extract(src, m, tmp)
                tmp.seek(0)
                tmp_src = FileRange.__new__(FileRange)
                tmp_src.f, tmp_src.len, tmp_src.bytes_read = tmp, len(tmp.getvalue()), 0
                h, n = walk_zip(tmp_src, f"{label}:{m.base}", stream_nested, max_size, log_members, kinds, seen)
                hits += h
                notes += n
            else:
                notes.append(f"{m.base} is deflated inside {label} ({m.csize} bytes); "
                             "pass --stream-nested to read through it")
    return hits, notes


def is_fbpk(name):
    base = name.rsplit("/", 1)[-1].lower()
    return base.endswith(".img") and base.startswith(("bootloader", "radio"))


# Titan M firmware carries a build tag as text and a Cr50-style SignedHeader
# (1024 bytes, magic fffffffd on a Titan M2) with epoch/major/minor at 860.
TAG_RE = re.compile(rb"nugget_v\d+\.\d+\.ab(\d+)-([0-9a-f]{7,40})(?:-[a-z]+)?")
TITAN_MAGIC = (b"\xfd\xff\xff\xff", b"\xfe\xff\xff\xff")


def titan_info(data):
    """{tag, build, version} read out of a Titan firmware blob, where known."""
    info = {}
    m = TAG_RE.search(data)
    if m:
        info["tag"] = m.group(0).decode()
        info["build"] = int(m.group(1))
    versions = []
    for off in range(0, len(data) - 1024, 4):
        if data[off:off + 4] in TITAN_MAGIC:
            size, ro_base, ro_max, rx_base, rx_max = struct.unpack("<IIIII", data[off + 808:off + 828])
            epoch, major, minor = struct.unpack("<III", data[off + 860:off + 872])
            timestamp = struct.unpack("<Q", data[off + 872:off + 880])[0]
            p4cl = struct.unpack("<I", data[off + 880:off + 884])[0]
            if 1024 <= size <= len(data) - off and ro_base < ro_max and max(epoch, major, minor) < 0x10000:
                versions.append({"offset": off, "version": f"{epoch}.{major}.{minor}", "image_size": size,
                                 "ro_base": ro_base, "ro_max": ro_max, "rx_base": rx_base, "rx_max": rx_max,
                                 "timestamp": timestamp, "p4cl": p4cl,
                                 "chip": "dauntless" if data[off] == 0xFD else "citadel"})
    if versions:
        info["headers"] = versions
    tags = sorted({m.group(0).decode() for m in TAG_RE.finditer(data)})
    if len(tags) > 1:
        info["tags"] = tags
    return info


def fbpk_info(data):
    """{platform, pack_version, partitions} of an FBPK v2 pack."""
    if len(data) < 112 or data[:4] != b"FBPK" or struct.unpack("<I", data[4:8])[0] != 2:
        return {}
    cstr = lambda b: b.split(b"\0", 1)[0].decode("ascii", "replace")
    return {"platform": cstr(data[16:32]), "pack_version": cstr(data[32:96]).strip(),
            "partitions": [n for n, _ in fbpk_entries(data)]}


def open_source(url_or_path):
    if re.match(r"^https?://", url_or_path):
        return HttpRange(url_or_path)
    return FileRange(url_or_path)


# ----------------------------------------------------------------- commands

def device_index(index):
    """[{codename, name, family, soc, platform, year, gsc, gsc_chip, ...}] for
    every codename on the page, from the curated table plus what the listing
    shows (image count, first and last release)."""
    rows = []
    known = {r[0]: r for r in DEVICE_ROWS}
    for code in sorted(set(index) | set(known)):
        r = known.get(code)
        entry = index.get(code, {})
        images = entry.get("images", [])
        gsc = r[2] if r else entry.get("gsc")
        rows.append({
            "codename": code,
            "name": r[1] if r else entry.get("name", code),
            "family": r[6] if r else None,
            "soc": r[3] if r else None,
            "platform": r[4] if r else None,
            "year": r[5] if r else None,
            "gsc": gsc,
            "gsc_chip": {"citadel": "Titan M", "dauntless": "Titan M2"}.get(gsc),
            "gsc_flavor": GSC_FLAVOR.get(code),
            "inferred": code in INFERRED,
            "on_listing": code in index,
            "images": len(images),
            "first_release": images[0]["description"] if images else None,
            "latest_release": images[-1]["description"] if images else None,
            "latest_build": images[-1]["build"] if images else None,
        })
    return rows


def cmd_index(args):
    try:
        index = parse_page(listing_html(args.page))
    except (OSError, urllib.error.URLError) as e:
        fail(f"reading the listing: {e}")
    everything = index
    if args.device:
        want = {d.lower() for d in args.device}
        index = {k: v for k, v in index.items() if k in want or v["name"].lower() in want}
        missing = want - {k for k in index} - {v["name"].lower() for v in index.values()}
        if missing:
            fail(f"not on the page: {', '.join(sorted(missing))}")
    if not index:
        fail(f"no factory image links found; is the page behind the terms wall?")
    if args.gsc_only:
        index = {k: v for k, v in index.items() if v["gsc"] in ("citadel", "dauntless")}
    total = sum(len(v["images"]) for v in index.values())
    doc = {"source": IMAGES_PAGE, "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "devices": len(index), "images": total, "by_codename": dict(sorted(index.items()))}
    text = json.dumps(doc, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
        log(f"wrote {args.out}: {len(index)} devices, {total} images")
    else:
        print(text)
    if args.devices_out:
        rows = device_index(everything)
        Path(args.devices_out).write_text(json.dumps(
            {"source": IMAGES_PAGE, "fetched": doc["fetched"], "devices": rows}, indent=2) + "\n")
        log(f"wrote {args.devices_out}: {len(rows)} devices")


def cmd_list(args):
    try:
        src = open_source(args.image)
        log(f"{args.image}: {src.len / 1e9:.2f} GB")
        hits, notes = walk_zip(src, args.image.rsplit('/', 1)[-1], args.stream_nested,
                               args.max_size, log_members=True)
    except (OSError, ValueError, struct.error, urllib.error.URLError) as e:
        fail(f"{args.image}: {e}")
    for h in hits:
        log(f"GSC: {h.where} ({len(h.data)} bytes, sha256 {h.sha256[:16]}...)")
    for n in notes:
        log(f"note: {n}")
    log(f"{src.bytes_read / 1e6:.1f} MB transferred")


def list_members(src, label, log_members=False):
    """[(member dicts)] of a ZIP and, for every stored nested ZIP, its own
    members under "nested", read in place."""
    out = []
    for m in zip_members(src):
        d = m.as_dict()
        if log_members:
            log(f"  {m.usize:>12}  {'stored' if m.method == 0 else 'deflate'}  {m.name}")
        if m.base.lower().endswith(".zip") and m.method == 0 and not m.name.endswith("/"):
            inner = SubRange(src, member_data_offset(src, m), m.usize)
            try:
                d["nested"] = list_members(inner, f"{label}:{m.base}", log_members)
            except (ValueError, struct.error) as e:
                d["nested_error"] = str(e)
        out.append(d)
    return out


def listing_for(img, code, backend, pz):
    """One image's complete file list, read by byte range."""
    entry = {"codename": code, "build": img["build"], "url": img["url"], "file": img["file"],
             "sha256": img["sha256"], "description": img["description"], "carrier": img["carrier"],
             "listed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if pz and backend == "partialzip":
        entry["backend"] = "partialzip"
        entry["members"] = [{"name": n} for n in partialzip_list(pz, img["url"])]
    else:
        src = HttpRange(img["url"])
        entry["backend"] = "builtin"
        entry["size"] = src.len
        entry["members"] = list_members(src, img["file"])
        entry["bytes_transferred"] = src.bytes_read
    names = []
    for m in entry["members"]:
        names.append(m["name"])
        for n in m.get("nested", []):
            names.append(f"{m['name']}:{n['name']}")
    entry["gsc_candidates"] = [n for n in names if is_gsc(n.rsplit(":", 1)[-1])]
    return entry


def cmd_listings(args):
    doc = json.loads(Path(args.index).read_text())
    by = doc["by_codename"]
    if args.device:
        want = {d.lower() for d in args.device}
        by = {k: v for k, v in by.items() if k in want or v["name"].lower() in want}
    out = Path(args.out)
    todo = []
    for code, v in by.items():
        if not args.all and v["gsc"] not in ("citadel", "dauntless"):
            continue
        for img in v["images"]:
            dest = out / code / f"{img['build']}.json"
            if dest.is_file() and not args.refresh:
                continue
            todo.append((code, img, dest))
    if args.limit:
        todo = todo[-args.limit:]
    pz = partialzip_bin(args.partialzip) if args.backend != "builtin" else None
    if args.backend == "partialzip" and not pz:
        fail("partialzip not found; install it or use --backend builtin")
    backend = "partialzip" if args.backend == "partialzip" else "builtin"
    log(f"{len(todo)} images to list into {out} ({backend}, {args.jobs} at a time)")
    failures = 0

    def one(item):
        code, img, dest = item
        try:
            entry = listing_for(img, code, backend, pz)
        except Exception as e:  # noqa: BLE001 - recorded, the sweep goes on
            return code, img, dest, None, str(e)
        return code, img, dest, entry, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for code, img, dest, entry, err in pool.map(one, todo):
            if err:
                failures += 1
                log(f"FAILED {code}/{img['build']}: {err}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(entry, indent=1) + "\n")
            n = len(entry["members"]) + sum(len(m.get("nested", [])) for m in entry["members"])
            log(f"{code}/{img['build']}: {n} files, {len(entry['gsc_candidates'])} GSC candidates, "
                f"{entry.get('bytes_transferred', 0) / 1e6:.1f} MB")
    log(f"done: {len(todo) - failures} listed, {failures} failed")
    if failures:
        sys.exit(1)


def cmd_report(args):
    """Group everything `fetch` gathered by chip, flavor and SHA-256."""
    out = Path(args.out)
    manifest = json.loads((out / "manifest.json").read_text())
    devices = {}
    if args.devices:
        devices = {r["codename"]: r for r in json.loads(Path(args.devices).read_text())["devices"]}
    blobs = {}
    for e in manifest["images"].values():
        for f in e.get("files", []):
            b = blobs.setdefault(f["sha256"], {"sha256": f["sha256"], "kind": f["kind"], "size": f["size"],
                                               "name": f["path"].rsplit("/", 1)[-1], "releases": []})
            b["releases"].append({"codename": e["codename"], "build": e["build"],
                                  "description": e["description"], "path": f["path"]})
    for b in blobs.values():
        path = out / b["releases"][0]["path"]
        data = path.read_bytes() if path.is_file() else b""
        code = b["releases"][0]["codename"]
        b["gsc"] = devices.get(code, {}).get("gsc") or manifest.get("gsc", {}).get(code)
        if b["kind"] == "gsc":
            b["flavor"] = re.sub(r"\.(ec\.)?(bin|rec)$", "", b["name"].lower())
            b.update(titan_info(data))
            chips = {h["chip"] for h in b.get("headers", [])}
            if chips:
                b["gsc"] = chips.pop() if len(chips) == 1 else "mixed"
        elif b["kind"] in ("bootloader", "radio"):
            b.update(fbpk_info(data))
        b["codenames"] = sorted({r["codename"] for r in b["releases"]})
        b["first_release"] = b["releases"][0]["description"]
        b["last_release"] = b["releases"][-1]["description"]
    groups = {}
    for b in blobs.values():
        key = f"{b['kind']}/{b.get('gsc') or 'unknown'}/{b.get('flavor') or b.get('platform') or '-'}"
        groups.setdefault(key, []).append(b)
    for key, items in sorted(groups.items()):
        log(f"== {key}: {len(items)} distinct blobs")
        for b in sorted(items, key=lambda b: (b.get("build", 0), b["first_release"])):
            ver = ", ".join(h["version"] for h in b.get("headers", [])) or b.get("pack_version", "")
            log(f"  {b['sha256'][:16]}  {b['size']:>9}  {ver:<24} {b.get('tag', '')} "
                f"x{len(b['releases'])} {','.join(b['codenames'])}  {b['first_release']} .. {b['last_release']}")
    report = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "images": len(manifest["images"]), "distinct_blobs": len(blobs),
              "groups": {k: sorted(v, key=lambda b: (b.get("build", 0), b["first_release"])) for k, v in sorted(groups.items())}}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    log(f"wrote {out / 'report.json'}: {len(blobs)} distinct blobs in {len(groups)} groups")


def cmd_fetch(args):
    doc = json.loads(Path(args.index).read_text())
    by = doc["by_codename"]
    if args.device:
        want = {d.lower() for d in args.device}
        by = {k: v for k, v in by.items() if k in want or v["name"].lower() in want}
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    for k in kinds:
        if k not in KINDS:
            fail(f"unknown kind {k}; choose from {', '.join(KINDS)}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {"images": {}}
    done = manifest["images"]
    manifest.setdefault("gsc", {}).update({k: v["gsc"] for k, v in by.items()})
    # Blobs already on disk by SHA-256: a bootloader shared by ten releases is
    # stored once and hard-linked into the other nine (every copy is still
    # read and hashed, so the manifest's SHA-256s are all measured).
    seen = {}
    for e in done.values():
        for f in e.get("files", []):
            if not f.get("same_as"):
                seen.setdefault(f["sha256"], f)
    todo = [(code, img) for code, v in by.items() for img in v["images"]
            if (not args.no_carriers or not img["carrier"]) and (args.gsc_all or v["gsc"] in ("citadel", "dauntless"))]
    if args.limit:
        todo = todo[-args.limit:]
    log(f"{len(todo)} images to read for {', '.join(kinds)}; "
        f"{sum(1 for _, i in todo if i['file'] in done)} already in the manifest")
    pz = partialzip_bin(args.partialzip) if args.backend in ("auto", "partialzip") else None
    if args.backend == "partialzip" and not pz:
        fail("partialzip not found; install it or use --backend builtin")
    for code, img in todo:
        if img["file"] in done and not args.refresh:
            continue
        label = f"{code}/{img['build']}"
        log(f"== {label} ({img['file']})")
        entry = {"codename": code, "build": img["build"], "url": img["url"],
                 "description": img["description"], "files": [], "notes": []}
        try:
            if pz:
                entry["backend"] = "partialzip"
                hits, notes = fetch_with_partialzip(pz, img["url"], out / "tmp", args, kinds, seen)
            else:
                entry["backend"] = "builtin"
                src = HttpRange(img["url"])
                hits, notes = walk_zip(src, img["file"], args.stream_nested, args.max_size,
                                       kinds=kinds, seen=seen)
                entry["bytes_transferred"] = src.bytes_read
        except Exception as e:  # noqa: BLE001 - keep going; the manifest records the failure
            entry["error"] = str(e)
            log(f"  failed: {e}")
            done[img["file"]] = entry
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            continue
        for h in hits:
            rel = Path(code) / img["build"] / h.where.split(":", 1)[1].replace(":", "/")
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            rec = {"path": rel.as_posix(), "where": h.where, "kind": h.kind, "size": h.size,
                   "sha256": h.sha256}
            if h.kind == "gsc":
                rec.update(titan_info(h.data))
            prev = seen.get(h.sha256)
            if prev and prev["path"] != rec["path"] and (out / prev["path"]).is_file():
                rec["same_as"] = prev["path"]
                if not dest.exists():
                    try:
                        os.link(out / prev["path"], dest)
                    except OSError:
                        dest.write_bytes(h.data)
                log(f"  {rel} (same as {prev['path']})")
            else:
                dest.write_bytes(h.data)
                seen.setdefault(h.sha256, rec)
                log(f"  {rel} ({h.size} bytes)")
            entry["files"].append(rec)
        entry["notes"] = notes
        for n in notes:
            log(f"  note: {n}")
        if not hits:
            log("  nothing of those kinds at the ZIP layers (GSC firmware may be inside vendor.img)")
        done[img["file"]] = entry
        (out / code / f"{img['build']}.json").write_text(json.dumps(entry, indent=2) + "\n")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    uniq = {}
    for e in done.values():
        for f in e.get("files", []):
            uniq.setdefault(f["sha256"], []).append(f["path"])
    log(f"manifest: {len(done)} images, {sum(len(e.get('files', [])) for e in done.values())} files, "
        f"{len(uniq)} distinct blobs -> {manifest_path}")


def fetch_with_partialzip(binary, url, tmp, args, kinds=("gsc",), seen=None):
    """The same walk through the partialzip CLI: list, then download the
    members that are wanted, nested ZIPs, or FBPK images."""
    tmp.mkdir(parents=True, exist_ok=True)
    hits, notes = [], []
    label = url.rsplit("/", 1)[-1]
    for name in partialzip_list(binary, url):
        base = name.rsplit("/", 1)[-1]
        kind = kind_of(name, kinds)
        if kind or base.lower().endswith(".zip") or ("gsc" in kinds and is_fbpk(name)):
            dest = tmp / base
            partialzip_download(binary, url, name, dest)
            data = dest.read_bytes()
            if kind:
                hits.append(Hit(f"{label}:{name}", data, kind))
            if base.lower().endswith(".zip"):
                h, n = walk_zip(FileRange(dest), f"{label}:{base}", True, args.max_size, kinds=kinds, seen=seen)
                hits += h
                notes += n
            elif "gsc" in kinds and is_fbpk(name):
                for pname, body in fbpk_entries(data):
                    if GSC_NAME.search(pname + ".bin"):
                        hits.append(Hit(f"{label}:{base}:{pname}", body, "gsc"))
            dest.unlink()
    return hits, notes


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("index", help="write a JSON index of every factory image URL by codename")
    i.add_argument("--out", help="write JSON here instead of stdout")
    i.add_argument("--page", help="parse a saved copy of the listing page instead of fetching it")
    i.add_argument("--device", action="append", help="only these codenames or names (repeatable)")
    i.add_argument("--gsc-only", action="store_true", help="only devices with a Titan M / M2")
    i.add_argument("--devices-out", help="also write device-index.json here (codename, SoC, GSC, ...)")
    i.set_defaults(func=cmd_index)
    l = sub.add_parser("list", help="show the members of one factory ZIP (URL or file) and any GSC files")
    l.add_argument("image")
    l.add_argument("--stream-nested", action="store_true", help="read through a deflated image-*.zip")
    l.add_argument("--max-size", type=int, default=16 << 20, help="largest firmware file to pull (bytes)")
    l.set_defaults(func=cmd_list)
    ls = sub.add_parser("listings", help="save every indexed image's file list as <out>/<codename>/<build>.json")
    ls.add_argument("--index", required=True, help="JSON from `index`")
    ls.add_argument("--out", required=True, help="folder for <codename>/<build>.json")
    ls.add_argument("--device", action="append", help="only these codenames or names (repeatable)")
    ls.add_argument("--all", action="store_true", help="also devices with no known GSC (Nexus, Pixel 1/2)")
    ls.add_argument("--limit", type=int, help="only the newest N images not yet listed")
    ls.add_argument("--refresh", action="store_true", help="re-list images that already have a file")
    ls.add_argument("--jobs", type=int, default=4, help="images to read at once")
    ls.add_argument("--backend", choices=("auto", "builtin", "partialzip"), default="auto",
                    help="partialzip lists only the outer ZIP; builtin (default) also lists the stored image-*.zip inside")
    ls.add_argument("--partialzip", help="path to the partialzip binary")
    ls.set_defaults(func=cmd_listings)
    r = sub.add_parser("report", help="group gathered blobs by chip/flavor/SHA-256 and decode each into report.json")
    r.add_argument("--out", required=True, help="the fetch output folder (holds manifest.json)")
    r.add_argument("--devices", help="device-index.json, for the chip of codenames the manifest lacks")
    r.set_defaults(func=cmd_report)
    f = sub.add_parser("fetch", help="pull every GSC firmware file out of the indexed images")
    f.add_argument("--index", required=True, help="JSON from `index`")
    f.add_argument("--out", required=True, help="folder for <codename>/<build>/... and manifest.json")
    f.add_argument("--device", action="append", help="only these codenames or names (repeatable)")
    f.add_argument("--kinds", default=",".join(KINDS),
                   help=f"comma-separated kinds to pull: {', '.join(KINDS)} (default all)")
    f.add_argument("--no-carriers", action="store_true", help="skip carrier-specific builds")
    f.add_argument("--gsc-all", action="store_true", help="also read devices with no known GSC")
    f.add_argument("--limit", type=int, help="only the newest N images (for a trial run)")
    f.add_argument("--refresh", action="store_true", help="re-read images already in the manifest")
    f.add_argument("--stream-nested", action="store_true", help="read through a deflated image-*.zip")
    f.add_argument("--max-size", type=int, default=16 << 20, help="largest .bin/.rec to pull (bytes)")
    f.add_argument("--backend", choices=("auto", "builtin", "partialzip"), default="auto")
    f.add_argument("--partialzip", help="path to the partialzip binary")
    f.set_defaults(func=cmd_fetch)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
