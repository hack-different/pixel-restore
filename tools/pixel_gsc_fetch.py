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

# Codename -> (marketing name, GSC). Pixel 3 through 5 carry a Titan M
# (Citadel, firmware ec.bin / rescue ec.rec); Tensor Pixels a Titan M2
# (Dauntless, firmware <flavor>.bin). Unlisted codenames on the page are
# indexed too, with gsc "unknown".
DEVICES = {
    "blueline": ("Pixel 3", "citadel"), "crosshatch": ("Pixel 3 XL", "citadel"),
    "sargo": ("Pixel 3a", "citadel"), "bonito": ("Pixel 3a XL", "citadel"),
    "flame": ("Pixel 4", "citadel"), "coral": ("Pixel 4 XL", "citadel"),
    "sunfish": ("Pixel 4a", "citadel"), "bramble": ("Pixel 4a 5G", "citadel"),
    "redfin": ("Pixel 5", "citadel"), "barbet": ("Pixel 5a", "citadel"),
    "oriole": ("Pixel 6", "dauntless"), "raven": ("Pixel 6 Pro", "dauntless"),
    "bluejay": ("Pixel 6a", "dauntless"),
    "panther": ("Pixel 7", "dauntless"), "cheetah": ("Pixel 7 Pro", "dauntless"),
    "lynx": ("Pixel 7a", "dauntless"), "felix": ("Pixel Fold", "dauntless"),
    "tangorpro": ("Pixel Tablet", "dauntless"),
    "shiba": ("Pixel 8", "dauntless"), "husky": ("Pixel 8 Pro", "dauntless"),
    "akita": ("Pixel 8a", "dauntless"),
    "tokay": ("Pixel 9", "dauntless"), "caiman": ("Pixel 9 Pro", "dauntless"),
    "komodo": ("Pixel 9 Pro XL", "dauntless"), "comet": ("Pixel 9 Pro Fold", "dauntless"),
    "tegu": ("Pixel 9a", "dauntless"),
    "frankel": ("Pixel 10", "dauntless"), "blazer": ("Pixel 10 Pro", "dauntless"),
    "mustang": ("Pixel 10 Pro XL", "dauntless"), "rango": ("Pixel 10 Pro Fold", "dauntless"),
    # Older Pixels and Nexus devices have no GSC; they are indexed for completeness.
    "sailfish": ("Pixel", None), "marlin": ("Pixel XL", None),
    "walleye": ("Pixel 2", None), "taimen": ("Pixel 2 XL", None),
}
CARRIERS = ("verizon", "t-mobile", "at&t", "telstra", "softbank", "kddi", "docomo",
            "emea", "jp", "tw", "sprint", "google fi", "rogers", "optus", "vodafone")
# GSC firmware file names inside an image: anything .rec, and .bin files named
# for a chip (ec.bin, d2.bin, d3.bin, d3m2.bin, ...), or anything under a
# citadel/dauntless/gsc directory.
GSC_NAME = re.compile(r"(^|/)(ec|d\d[a-z0-9]*|citadel|dauntless|gsc)(\.ec)?\.(bin|rec)$", re.I)
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
        entry["images"].sort(key=lambda i: build_key(i["build"]))
    return index


def build_key(build):
    """Newest-last order: the YYMMDD and number inside e.g. cp3a.260905.009."""
    parts = build.split(".")
    date = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    num = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return (date, num, build)


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


def is_fbpk(name):
    base = name.rsplit("/", 1)[-1].lower()
    return base.endswith(".img") and base.startswith(("bootloader", "radio"))


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
    def __init__(self, where, data):
        self.where, self.data = where, data
        self.sha256 = hashlib.sha256(data).hexdigest()


def walk_zip(src, label, stream_nested, max_size, log_members=False):
    """Every GSC firmware file reachable in a factory ZIP: top-level `.rec`
    and `.bin`, the same inside `image-*.zip` (read in place when stored,
    streamed through when deflated and `stream_nested`), and partitions named
    for a GSC inside FBPK `bootloader-*.img` / `radio-*.img`."""
    hits, notes = [], []
    members = zip_members(src)
    if log_members:
        for m in members:
            log(f"  {m.usize:>12}  {'stored' if m.method == 0 else 'deflate'}  {m.name}")
    for m in members:
        if m.name.endswith("/"):
            continue
        if is_gsc(m.name):
            if m.usize > max_size:
                notes.append(f"{m.name}: {m.usize} bytes is over --max-size, skipped")
                continue
            hits.append(Hit(f"{label}:{m.name}", extract(src, m)))
        elif m.base.lower().endswith(".zip"):
            if m.method == 0:
                inner = SubRange(src, member_data_offset(src, m), m.usize)
                h, n = walk_zip(inner, f"{label}:{m.base}", stream_nested, max_size, log_members)
                hits += h
                notes += n
            elif stream_nested:
                log(f"  {m.base} is deflated inside {label}; streaming {m.csize / 1e6:.0f} MB through ...")
                tmp = io.BytesIO()
                extract(src, m, tmp)
                tmp.seek(0)
                tmp_src = FileRange.__new__(FileRange)
                tmp_src.f, tmp_src.len, tmp_src.bytes_read = tmp, len(tmp.getvalue()), 0
                h, n = walk_zip(tmp_src, f"{label}:{m.base}", stream_nested, max_size, log_members)
                hits += h
                notes += n
            else:
                notes.append(f"{m.base} is deflated inside {label} ({m.csize} bytes); "
                             "pass --stream-nested to read through it")
        elif is_fbpk(m.name) and m.usize <= max(max_size, 64 << 20):
            data = extract(src, m)
            for name, body in fbpk_entries(data):
                if GSC_NAME.search(name + ".bin"):
                    hits.append(Hit(f"{label}:{m.base}:{name}", body))
    return hits, notes


def open_source(url_or_path):
    if re.match(r"^https?://", url_or_path):
        return HttpRange(url_or_path)
    return FileRange(url_or_path)


# ----------------------------------------------------------------- commands

def cmd_index(args):
    try:
        index = parse_page(listing_html(args.page))
    except (OSError, urllib.error.URLError) as e:
        fail(f"reading the listing: {e}")
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


def cmd_list(args):
    try:
        src = open_source(args.image)
        log(f"{args.image}: {src.len / 1e9:.2f} GB")
        hits, notes = walk_zip(src, args.image.rsplit('/', 1)[-1], args.stream_nested,
                               args.max_size, log_members=True)
    except (OSError, ValueError, urllib.error.URLError) as e:
        fail(f"{args.image}: {e}")
    for h in hits:
        log(f"GSC: {h.where} ({len(h.data)} bytes, sha256 {h.sha256[:16]}...)")
    for n in notes:
        log(f"note: {n}")
    log(f"{src.bytes_read / 1e6:.1f} MB transferred")


def cmd_fetch(args):
    doc = json.loads(Path(args.index).read_text())
    by = doc["by_codename"]
    if args.device:
        want = {d.lower() for d in args.device}
        by = {k: v for k, v in by.items() if k in want or v["name"].lower() in want}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {"images": {}}
    done = manifest["images"]
    todo = [(code, img) for code, v in by.items() for img in v["images"]
            if (args.carriers or not img["carrier"]) and (args.gsc_all or v["gsc"] != None)]
    if args.limit:
        todo = todo[-args.limit:]
    log(f"{len(todo)} images to read; {sum(1 for _, i in todo if i['file'] in done)} already in the manifest")
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
                hits, notes = fetch_with_partialzip(pz, img["url"], out / "tmp", args)
            else:
                entry["backend"] = "builtin"
                src = HttpRange(img["url"])
                hits, notes = walk_zip(src, img["file"], args.stream_nested, args.max_size)
                entry["bytes_transferred"] = src.bytes_read
        except Exception as e:  # keep going; the manifest records the failure
            entry["error"] = str(e)
            log(f"  failed: {e}")
            done[img["file"]] = entry
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            continue
        for h in hits:
            rel = Path(code) / img["build"] / h.where.split(":", 1)[1].replace(":", "/")
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(h.data)
            entry["files"].append({"path": str(rel), "where": h.where, "size": len(h.data),
                                   "sha256": h.sha256})
            log(f"  {rel} ({len(h.data)} bytes)")
        entry["notes"] = notes
        for n in notes:
            log(f"  note: {n}")
        if not hits:
            log("  no GSC firmware found at the ZIP layers (it may be inside vendor.img)")
        done[img["file"]] = entry
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    uniq = {}
    for e in done.values():
        for f in e.get("files", []):
            uniq.setdefault(f["sha256"], []).append(f["path"])
    log(f"manifest: {len(done)} images, {sum(len(e.get('files', [])) for e in done.values())} files, "
        f"{len(uniq)} distinct firmware blobs -> {manifest_path}")


def fetch_with_partialzip(binary, url, tmp, args):
    """The same walk through the partialzip CLI: list, then download the
    members that are GSC files, nested ZIPs, or FBPK images."""
    tmp.mkdir(parents=True, exist_ok=True)
    hits, notes = [], []
    names = partialzip_list(binary, url)
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if is_gsc(name) or base.lower().endswith(".zip") or is_fbpk(name):
            dest = tmp / base
            partialzip_download(binary, url, name, dest)
            if is_gsc(name):
                hits.append(Hit(f"{url.rsplit('/', 1)[-1]}:{name}", dest.read_bytes()))
            elif base.lower().endswith(".zip"):
                h, n = walk_zip(FileRange(dest), f"{url.rsplit('/', 1)[-1]}:{base}", True, args.max_size)
                hits += h
                notes += n
            else:
                for pname, body in fbpk_entries(dest.read_bytes()):
                    if GSC_NAME.search(pname + ".bin"):
                        hits.append(Hit(f"{url.rsplit('/', 1)[-1]}:{base}:{pname}", body))
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
    i.set_defaults(func=cmd_index)
    l = sub.add_parser("list", help="show the members of one factory ZIP (URL or file) and any GSC files")
    l.add_argument("image")
    l.add_argument("--stream-nested", action="store_true", help="read through a deflated image-*.zip")
    l.add_argument("--max-size", type=int, default=16 << 20, help="largest firmware file to pull (bytes)")
    l.set_defaults(func=cmd_list)
    f = sub.add_parser("fetch", help="pull every GSC firmware file out of the indexed images")
    f.add_argument("--index", required=True, help="JSON from `index`")
    f.add_argument("--out", required=True, help="folder for <codename>/<build>/... and manifest.json")
    f.add_argument("--device", action="append", help="only these codenames or names (repeatable)")
    f.add_argument("--carriers", action="store_true", help="include carrier-specific builds")
    f.add_argument("--gsc-all", action="store_true", help="also read devices with no known GSC")
    f.add_argument("--limit", type=int, help="only the newest N images (for a trial run)")
    f.add_argument("--refresh", action="store_true", help="re-read images already in the manifest")
    f.add_argument("--stream-nested", action="store_true", help="read through a deflated image-*.zip")
    f.add_argument("--max-size", type=int, default=16 << 20, help="largest firmware file to pull (bytes)")
    f.add_argument("--backend", choices=("auto", "builtin", "partialzip"), default="auto")
    f.add_argument("--partialzip", help="path to the partialzip binary")
    f.set_defaults(func=cmd_fetch)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
