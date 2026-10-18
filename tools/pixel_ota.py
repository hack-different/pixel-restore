#!/usr/bin/env python3
"""Pull partition images and Titan M / M2 (GSC) firmware out of Pixel OTA
packages without downloading them (Python 3.9+, standard library only; ZSTD
operations also need Python 3.14, the `zstandard` module or the zstd CLI).

An OTA ZIP from https://developers.google.com/android/ota is not a factory
image: it holds an A/B `payload.bin` (stored, so it is range-readable in
place) whose protobuf manifest lists every partition with its size, SHA-256
and the install operations that write it. In a full OTA every operation is a
REPLACE (raw, bzip2, xz or zstd blob) or ZERO, so any byte range of any
partition can be rebuilt from just the blobs that cover it:

  index     read the OTA listing page and write a JSON index of every OTA URL
            by codename (same layout as pixel_gsc_fetch.py's firmware index)
  list      print the partitions of one OTA (URL or file) with size and the
            manifest's SHA-256, reading only the ZIP directory and manifest
  listings  do that for every indexed OTA into <out>/<codename>/<build>.json
  extract   write named partitions of one OTA to a folder, verified against
            the manifest's SHA-256
  fetch     walk the index and pull the wanted kinds out of each OTA into the
            same folder layout and manifest.json as `pixel_gsc_fetch.py fetch`,
            so `pixel_gsc_fetch.py report` groups factory and OTA blobs together

Kinds: `bootloader` is every firmware partition (abl, bl1, bl2, bl31, gsa,
ldfw, tzsw, xbl, aop, ...: anything that is not an Android OS partition),
`radio` the modem partition, and `gsc` the Titan firmware files under
/firmware in the vendor partition, read through android_fs.py so only the
filesystem metadata and those files' blocks are fetched.

Incremental OTAs (SOURCE_COPY / *DIFF operations) need the source build's
partitions and are refused; Google publishes full OTAs only.

Reading the page or a package means accepting Google's OTA terms, the same as
clicking Acknowledge on the page. Nothing here touches a phone.
"""
import argparse
import bisect
import bz2
import concurrent.futures
import hashlib
import html as htmllib
import importlib.util
import io
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


g = _load("pixel_gsc_fetch")
afs = _load("android_fs")
log, fail = g.log, g.fail

OTA_PAGE = "https://developers.google.com/android/ota"
OTA_TOS_COOKIE = "devsite_wall_acks=nexus-ota-tos"

# Android OS partitions; every other partition in a Pixel payload is
# bootloader-chain firmware (Tensor: abl bl1 bl2 bl31 gcf gsa gsa_bl1 ldfw pbl
# tzsw; Snapdragon: abl aop devcfg hyp keymaster qupfw tz uefisecapp xbl ...).
OS_PARTITIONS = {
    "system", "system_ext", "system_dlkm", "product", "vendor", "vendor_dlkm",
    "odm", "odm_dlkm", "boot", "init_boot", "vendor_boot", "vendor_kernel_boot",
    "recovery", "dtbo", "vbmeta", "vbmeta_system", "vbmeta_vendor", "pvmfw",
}
RADIO_PARTITIONS = {"modem", "radio"}
KINDS = ("gsc", "bootloader", "radio")
GSC_PARTITIONS = ("vendor",)


# ---------------------------------------------------------------- protobuf

def pb_fields(buf):
    """(field number, value) for each field of a protobuf message: an int
    for varint and fixed fields, bytes for length-delimited ones."""
    p, n = 0, len(buf)

    def varint():
        nonlocal p
        shift = result = 0
        while True:
            if p >= n:
                raise ValueError("truncated protobuf varint")
            b = buf[p]
            p += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    while p < n:
        key = varint()
        field, wire = key >> 3, key & 7
        if wire == 0:
            yield field, varint()
        elif wire == 1:
            yield field, struct.unpack("<Q", buf[p:p + 8])[0]
            p += 8
        elif wire == 2:
            size = varint()
            if p + size > n:
                raise ValueError("truncated protobuf field")
            yield field, bytes(buf[p:p + size])
            p += size
        elif wire == 5:
            yield field, struct.unpack("<I", buf[p:p + 4])[0]
            p += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


# ------------------------------------------------------- update_engine payload

PAYLOAD_MAGIC = b"CrAU"
OP_NAMES = {0: "REPLACE", 1: "REPLACE_BZ", 2: "MOVE", 3: "BSDIFF", 4: "SOURCE_COPY",
            5: "SOURCE_BSDIFF", 6: "ZERO", 7: "DISCARD", 8: "REPLACE_XZ", 9: "PUFFDIFF",
            10: "BROTLI_BSDIFF", 11: "ZUCCHINI", 12: "LZ4DIFF_BSDIFF", 13: "LZ4DIFF_PUFFDIFF",
            14: "ZSTD"}
REPLACE_OPS = {0, 1, 8, 14}
ZERO_OPS = {6, 7}


class Operation:
    __slots__ = ("type", "data_offset", "data_length", "dst", "src", "data_sha256")

    def __init__(self, raw):
        self.type, self.data_offset, self.data_length = 0, 0, 0
        self.dst, self.src, self.data_sha256 = [], [], None
        for f, v in pb_fields(raw):
            if f == 1:
                self.type = v
            elif f == 2:
                self.data_offset = v
            elif f == 3:
                self.data_length = v
            elif f in (4, 6):
                ext = dict(pb_fields(v))
                (self.src if f == 4 else self.dst).append((ext.get(1, 0), ext.get(2, 0)))
            elif f == 8:
                self.data_sha256 = v

    @property
    def name(self):
        return OP_NAMES.get(self.type, f"type{self.type}")


class Partition:
    def __init__(self, raw):
        self.name, self.size, self.sha256, self.version = "", 0, None, None
        self.old_size, self.ops = None, []
        for f, v in pb_fields(raw):
            if f == 1:
                self.name = v.decode("utf-8", "replace")
            elif f == 6:
                self.old_size = dict(pb_fields(v)).get(1, 0)
            elif f == 7:
                info = dict(pb_fields(v))
                self.size = info.get(1, 0)
                self.sha256 = info[2].hex() if 2 in info else None
            elif f == 8:
                self.ops.append(Operation(v))
            elif f == 17:
                self.version = v.decode("utf-8", "replace")

    @property
    def full(self):
        return all(op.type in REPLACE_OPS or op.type in ZERO_OPS for op in self.ops)

    def as_dict(self):
        counts = {}
        for op in self.ops:
            counts[op.name] = counts.get(op.name, 0) + 1
        d = {"name": self.name, "size": self.size, "sha256": self.sha256,
             "data_bytes": sum(op.data_length for op in self.ops), "operations": counts}
        if self.version:
            d["version"] = self.version
        return d


class Payload:
    """The header and manifest of a payload.bin, read from any RangeSource."""

    def __init__(self, src):
        head = src.read(0, 24)
        if head[:4] != PAYLOAD_MAGIC:
            raise ValueError("not an update_engine payload (no CrAU magic)")
        self.version, manifest_size = struct.unpack(">QQ", head[4:20])
        if self.version != 2:
            raise ValueError(f"payload major version {self.version} is not supported (want 2)")
        sig_size = struct.unpack(">I", head[20:24])[0]
        self.src = src
        self.manifest_size = manifest_size
        self.data_start = 24 + manifest_size + sig_size
        manifest = src.read(24, 24 + manifest_size)
        self.block_size, self.minor_version = 4096, 0
        self.max_timestamp, self.security_patch_level = None, None
        self.partitions = []
        for f, v in pb_fields(manifest):
            if f == 3:
                self.block_size = v
            elif f == 12:
                self.minor_version = v
            elif f == 13:
                self.partitions.append(Partition(v))
            elif f == 14:
                self.max_timestamp = v
            elif f == 18:
                self.security_patch_level = v.decode("utf-8", "replace")

    @property
    def full(self):
        return all(p.full for p in self.partitions)

    def partition(self, name):
        for p in self.partitions:
            if p.name == name:
                return p
        raise KeyError(f"no partition {name} in the payload "
                       f"(have {', '.join(p.name for p in self.partitions)})")

    def info(self):
        return {"payload_version": self.version, "minor_version": self.minor_version,
                "full": self.full, "block_size": self.block_size,
                "max_timestamp": self.max_timestamp,
                "security_patch_level": self.security_patch_level,
                "partitions": [p.as_dict() for p in self.partitions]}

    def open(self, name):
        return PartitionRange(self, self.partition(name))


def _zstd(data):
    try:
        from compression import zstd  # Python 3.14
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    except ImportError:
        pass
    binary = shutil.which("zstd")
    if not binary:
        raise afs.Error("ZSTD operation: needs Python 3.14, the zstandard module or the zstd CLI")
    return subprocess.run([binary, "-dc"], input=data, capture_output=True, check=True).stdout


def decode_op(op, blob):
    if op.type == 0:
        return blob
    if op.type == 1:
        return bz2.decompress(blob)
    if op.type == 8:
        return lzma.decompress(blob)
    if op.type == 14:
        return _zstd(blob)
    raise afs.Error(f"{op.name} operation needs the source build (incremental OTA)")


class PartitionRange(afs.Range):
    """One partition of a full payload as a byte-range source: a read only
    fetches and decompresses the operations whose destination blocks it
    touches, so a filesystem inside can be walked without the whole image."""

    def __init__(self, payload, part, src=None, cache_ops=8):
        self.payload, self.part, self.len = payload, part, part.size
        self.src = src or payload.src
        self.cache, self.cache_ops = {}, cache_ops
        bs = payload.block_size
        pieces = []  # (partition offset, length, op index, offset in the op's output)
        for i, op in enumerate(part.ops):
            out = 0
            for start, count in op.dst:
                pieces.append((start * bs, count * bs, i, out))
                out += count * bs
        pieces.sort()
        self.pieces = pieces
        self.starts = [p[0] for p in pieces]

    def _op_output(self, i):
        if i in self.cache:
            return self.cache[i]
        op = self.part.ops[i]
        base = self.payload.data_start + op.data_offset
        blob = self.src.read(base, base + op.data_length)
        if op.data_sha256 and hashlib.sha256(blob).digest() != op.data_sha256:
            raise afs.Error(f"{self.part.name}: operation {i} data fails its SHA-256")
        data = decode_op(op, blob)
        if len(self.cache) >= self.cache_ops:
            self.cache.pop(next(iter(self.cache)))
        self.cache[i] = data
        return data

    def read(self, start, end):
        end = min(end, self.len)
        if start >= end:
            return b""
        out, pos = [], start
        k = max(0, bisect.bisect_right(self.starts, start) - 1)
        while pos < end and k < len(self.pieces):
            pstart, plen, i, ooff = self.pieces[k]
            k += 1
            if pstart + plen <= pos:
                continue
            if pstart > pos:  # blocks no operation writes read as zeros
                gap = min(pstart, end) - pos
                out.append(b"\0" * gap)
                pos += gap
                if pos >= end:
                    break
            take = min(end, pstart + plen) - pos
            op = self.part.ops[i]
            if op.type in ZERO_OPS:
                out.append(b"\0" * take)
            else:
                data = self._op_output(i)
                lo = ooff + (pos - pstart)
                chunk = data[lo:lo + take]
                out.append(chunk + b"\0" * (take - len(chunk)))
            pos += take
        if pos < end:
            out.append(b"\0" * (end - pos))
        return b"".join(out)


class ReadAhead(g.RangeSource):
    """Coalesces the many small sequential reads of a whole-partition
    extraction into a few large range requests."""

    def __init__(self, src, window=32 << 20):
        self.src, self.len, self.window = src, src.len, window
        self.buf_start, self.buf = 0, b""

    @property
    def bytes_read(self):
        return self.src.bytes_read

    def read(self, start, end):
        if not (self.buf_start <= start and end <= self.buf_start + len(self.buf)):
            if end - start >= self.window:
                return self.src.read(start, end)
            self.buf_start = start
            self.buf = self.src.read(start, min(self.len, start + self.window))
        return self.buf[start - self.buf_start:end - self.buf_start]


def dump_partition(payload, name, sink, chunk=8 << 20):
    """Write a whole partition to `sink`; return (size, SHA-256 hex, verified)."""
    part = payload.partition(name)
    rng = PartitionRange(payload, part, src=ReadAhead(payload.src))
    h = hashlib.sha256()
    for pos in range(0, part.size, chunk):
        data = rng.read(pos, min(part.size, pos + chunk))
        h.update(data)
        sink.write(data)
    digest = h.hexdigest()
    if part.sha256 and digest != part.sha256:
        raise afs.Error(f"{name}: SHA-256 {digest} does not match the manifest's {part.sha256}")
    return part.size, digest, part.sha256 is not None


# --------------------------------------------------------------- OTA package

class Ota:
    """An OTA ZIP (URL or file): its payload, payload_properties.txt and
    META-INF/com/android/metadata, read by byte range."""

    def __init__(self, url_or_path):
        self.name = url_or_path.rsplit("/", 1)[-1]
        self.zip = g.open_source(url_or_path)
        self.members = {m.name: m for m in g.zip_members(self.zip)}
        m = self.members.get("payload.bin")
        if m is None:
            raise ValueError("no payload.bin in the ZIP (not an A/B OTA)")
        if m.method == 0:
            src = g.SubRange(self.zip, g.member_data_offset(self.zip, m), m.usize)
        else:
            log(f"  payload.bin is deflated; streaming {m.csize / 1e6:.0f} MB through ...")
            self._tmp = tempfile.TemporaryFile()
            g.extract(self.zip, m, self._tmp)
            src = g.FileRange.__new__(g.FileRange)
            src.f, src.len, src.bytes_read = self._tmp, m.usize, 0
        self.payload = Payload(src)

    def text(self, name):
        m = self.members.get(name)
        return g.extract(self.zip, m).decode("utf-8", "replace") if m and m.usize < 1 << 20 else None

    def properties(self):
        out = {}
        for name in ("payload_properties.txt", "META-INF/com/android/metadata"):
            for line in (self.text(name) or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        return out

    @property
    def bytes_read(self):
        return self.zip.bytes_read


def partition_kind(name, kinds):
    if "radio" in kinds and name in RADIO_PARTITIONS:
        return "radio"
    if "bootloader" in kinds and name not in OS_PARTITIONS and name not in RADIO_PARTITIONS:
        return "bootloader"
    return None


def walk_ota(ota, kinds, partitions=(), max_size=16 << 20):
    """(hits, notes) for one OTA: whole partitions of the wanted kinds (and
    any named in `partitions`), plus GSC files from the vendor filesystem.
    Each hit's `where` is `<ota>:payload.bin:<partition>.img` or
    `<ota>:payload.bin:vendor/firmware/...`."""
    hits, notes = [], []
    p = ota.payload
    if not p.full:
        notes.append("incremental payload: only partitions written entirely by REPLACE/ZERO are readable")
    for part in p.partitions:
        kind = partition_kind(part.name, kinds) or ("partition" if part.name in partitions else None)
        if not kind:
            continue
        if not part.full:
            notes.append(f"{part.name}: needs the source build (incremental operations), skipped")
            continue
        buf = io.BytesIO()
        size, digest, verified = dump_partition(p, part.name, buf)
        hit = g.Hit(f"{ota.name}:payload.bin:{part.name}.img", buf.getvalue(), kind)
        hit.extra = {"partition": part.name, "manifest_sha256": part.sha256, "verified": verified}
        hits.append(hit)
    if "gsc" in kinds:
        for name in GSC_PARTITIONS:
            try:
                part = p.partition(name)
            except KeyError:
                continue
            try:
                for path, size, reader in afs.find_files(p.open(name), g.is_gsc, root_hint="/firmware"):
                    if size > max_size:
                        notes.append(f"{name}{path}: {size} bytes is over --max-size, skipped")
                        continue
                    hit = g.Hit(f"{ota.name}:payload.bin:{name}{path}", reader(), "gsc")
                    hit.extra = {"partition": name, "fs_path": path}
                    hits.append(hit)
            except afs.Error as e:
                notes.append(f"{name}: {e}")
    return hits, notes


# ----------------------------------------------------------------- listing page

def parse_ota_page(html):
    """{codename: {"name", "gsc", "images": [...]}} from the OTA listing, the
    same shape as pixel_gsc_fetch.parse_page. Every OTA is a
    `LINK_PREFIX<codename>-ota-<build>-<id>.zip` link with its SHA-256 after it."""
    names = {}
    for m in re.finditer(r'<h2[^>]*id="([a-z0-9]+)"[^>]*>(.*?)</h2>', html, re.I | re.S):
        text = g.strip_tags(m.group(2))
        q = re.search(r'"[^"]+"\s+for\s+(.+)$', text)
        names[m.group(1).lower()] = q.group(1).strip() if q else text
    index, seen = {}, set()
    pat = re.escape(g.LINK_PREFIX) + r'([a-z0-9]+)-ota-([^"\s<>]+?)-([0-9a-f]+)\.zip'
    for m in re.finditer(pat, html, re.I):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        codename, build = m.group(1).lower(), m.group(2).lower()
        window = html[m.end():m.end() + 4000]
        sha = next((t.lower() for t in re.split(r"[^0-9a-fA-F]", window) if len(t) == 64), None)
        row_start = html.rfind("<tr", 0, m.start())
        link_start = html.rfind("<a", 0, m.start())
        desc = g.strip_tags(html[row_start:link_start]) if 0 <= row_start < link_start else ""
        desc = re.sub(r"\s*(Link|Flash)\s*$", "", desc).strip()
        # entities may be double-escaped on the page ("AT&amp;amp;T")
        desc = htmllib.unescape(htmllib.unescape(desc))
        low = desc.lower()
        carrier = any(f"({c}" in low or f", {c}" in low for c in g.CARRIERS)
        entry = index.setdefault(codename, {
            "name": g.DEVICES.get(codename, (names.get(codename, codename), None))[0],
            "gsc": g.DEVICES[codename][1] if codename in g.DEVICES else "unknown",
            "images": [],
        })
        entry["images"].append({"build": build, "url": url, "sha256": sha, "description": desc,
                                "carrier": carrier, "file": url.rsplit("/", 1)[1], "type": "ota"})
    for entry in index.values():
        entry["images"].sort(key=lambda i: g.release_key(i["description"], i["build"]))
    return index


def ota_html(page_file=None):
    if page_file:
        return Path(page_file).read_text(errors="replace")
    log(f"reading {OTA_PAGE} (this accepts Google's OTA terms) ...")
    with g.http_get(OTA_PAGE, {"Cookie": OTA_TOS_COOKIE}) as r:
        return r.read().decode("utf-8", "replace")


def select(by, devices):
    if not devices:
        return by
    want = {d.lower() for d in devices}
    return {k: v for k, v in by.items() if k in want or v["name"].lower() in want}


# ----------------------------------------------------------------- commands

def cmd_index(args):
    try:
        index = parse_ota_page(ota_html(args.page))
    except (OSError, urllib.error.URLError) as e:
        fail(f"reading the listing: {e}")
    index = select(index, args.device)
    if not index:
        fail("no OTA links found; is the page behind the terms wall?")
    if args.gsc_only:
        index = {k: v for k, v in index.items() if v["gsc"] in ("citadel", "dauntless")}
    total = sum(len(v["images"]) for v in index.values())
    doc = {"source": OTA_PAGE, "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "devices": len(index), "images": total, "by_codename": dict(sorted(index.items()))}
    text = json.dumps(doc, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
        log(f"wrote {args.out}: {len(index)} devices, {total} OTAs")
    else:
        print(text)


def describe(ota):
    d = {"file": ota.name, "zip_size": ota.zip.len, "properties": ota.properties()}
    d.update(ota.payload.info())
    return d


def cmd_list(args):
    try:
        ota = Ota(args.ota)
        d = describe(ota)
    except (OSError, ValueError, struct.error, urllib.error.URLError) as e:
        fail(f"{args.ota}: {e}")
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        log(f"{ota.name}: {ota.zip.len / 1e9:.2f} GB, payload v{d['payload_version']} "
            f"minor {d['minor_version']} ({'full' if d['full'] else 'incremental'}), "
            f"patch level {d['security_patch_level']}")
        for p in d["partitions"]:
            kind = partition_kind(p["name"], KINDS) or ""
            ops = ",".join(f"{k}x{v}" for k, v in sorted(p["operations"].items()))
            log(f"  {p['name']:<20} {p['size']:>12}  {p['sha256'] or '-'}  {kind:<10} {ops}")
    log(f"{ota.bytes_read / 1e6:.2f} MB transferred")


def cmd_listings(args):
    by = select(json.loads(Path(args.index).read_text())["by_codename"], args.device)
    out = Path(args.out)
    todo = []
    for code, v in by.items():
        for img in v["images"]:
            dest = out / code / f"{img['build']}.json"
            if args.refresh or not dest.is_file():
                todo.append((code, img, dest))
    if args.limit:
        todo = todo[-args.limit:]
    log(f"{len(todo)} OTAs to list into {out} ({args.jobs} at a time)")

    def one(item):
        code, img, dest = item
        try:
            ota = Ota(img["url"])
            entry = {"codename": code, "build": img["build"], "url": img["url"],
                     "sha256": img["sha256"], "description": img["description"],
                     "carrier": img["carrier"], "listed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            entry.update(describe(ota))
            entry["bytes_transferred"] = ota.bytes_read
            return item, entry, None
        except Exception as e:  # noqa: BLE001 - recorded, the sweep goes on
            return item, None, str(e)

    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for (code, img, dest), entry, err in pool.map(one, todo):
            if err:
                failures += 1
                log(f"FAILED {code}/{img['build']}: {err}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(entry, indent=1) + "\n")
            log(f"{code}/{img['build']}: {len(entry['partitions'])} partitions, "
                f"{entry['bytes_transferred'] / 1e6:.2f} MB")
    log(f"done: {len(todo) - failures} listed, {failures} failed")
    if failures:
        sys.exit(1)


def cmd_extract(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        ota = Ota(args.ota)
        names = args.partition or [p.name for p in ota.payload.partitions
                                   if partition_kind(p.name, KINDS)]
        for name in names:
            dest = out / f"{name}.img"
            with open(dest, "wb") as f:
                size, digest, verified = dump_partition(ota.payload, name, f)
            log(f"{dest}: {size} bytes, sha256 {digest}{' (matches manifest)' if verified else ''}")
    except (OSError, ValueError, KeyError, afs.Error, urllib.error.URLError) as e:
        fail(f"{args.ota}: {e}")
    log(f"{ota.bytes_read / 1e6:.1f} MB transferred")


def record_hits(out, code, build, hits, seen):
    """Write hits under <out>/<codename>/<build>/..., hard-linking a blob
    already on disk by SHA-256; return the manifest records (the same fields
    as pixel_gsc_fetch.py fetch, plus partition and manifest_sha256)."""
    recs = []
    for h in hits:
        rel = Path(code) / build / h.where.split(":", 1)[1].replace(":", "/")
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        rec = {"path": rel.as_posix(), "where": h.where, "kind": h.kind, "size": h.size,
               "sha256": h.sha256, "source": "ota"}
        rec.update(getattr(h, "extra", {}))
        if h.kind == "gsc":
            rec.update(g.titan_info(h.data))
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
        recs.append(rec)
    return recs


def cmd_fetch(args):
    by = select(json.loads(Path(args.index).read_text())["by_codename"], args.device)
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    for k in kinds:
        if k not in KINDS:
            fail(f"unknown kind {k}; choose from {', '.join(KINDS)}")
    partitions = tuple(args.partition or ())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {"images": {}}
    done = manifest["images"]
    manifest.setdefault("gsc", {}).update({k: v["gsc"] for k, v in by.items()})
    seen = {}
    for e in done.values():
        for f in e.get("files", []):
            if not f.get("same_as"):
                seen.setdefault(f["sha256"], f)
    todo = [(code, img) for code, v in by.items() for img in v["images"]
            if (not args.no_carriers or not img["carrier"]) and (args.gsc_all or v["gsc"] in ("citadel", "dauntless"))]
    if args.limit:
        todo = todo[-args.limit:]
    log(f"{len(todo)} OTAs to read for {', '.join(kinds + partitions)}; "
        f"{sum(1 for _, i in todo if i['file'] in done)} already in the manifest")
    for code, img in todo:
        if img["file"] in done and not args.refresh:
            continue
        log(f"== {code}/{img['build']} ({img['file']})")
        entry = {"codename": code, "build": img["build"], "url": img["url"], "source": "ota",
                 "description": img["description"], "files": [], "notes": []}
        try:
            ota = Ota(img["url"])
            entry["security_patch_level"] = ota.payload.security_patch_level
            hits, notes = walk_ota(ota, kinds, partitions, args.max_size)
            entry["bytes_transferred"] = ota.bytes_read
        except Exception as e:  # noqa: BLE001 - keep going; the manifest records the failure
            entry["error"] = str(e)
            log(f"  failed: {e}")
            done[img["file"]] = entry
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            continue
        entry["files"] = record_hits(out, code, img["build"], hits, seen)
        entry["notes"] = notes
        for n in notes:
            log(f"  note: {n}")
        done[img["file"]] = entry
        (out / code / f"{img['build']}.ota.json").write_text(json.dumps(entry, indent=2) + "\n")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    moved = sum(e.get("bytes_transferred", 0) for e in done.values() if e.get("source") == "ota")
    log(f"manifest: {len(done)} images -> {manifest_path} ({moved / 1e6:.1f} MB transferred for OTAs)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("index", help="write a JSON index of every OTA URL by codename")
    i.add_argument("--out", help="write JSON here instead of stdout")
    i.add_argument("--page", help="parse a saved copy of the OTA listing page instead of fetching it")
    i.add_argument("--device", action="append", help="only these codenames or names (repeatable)")
    i.add_argument("--gsc-only", action="store_true", help="only devices with a Titan M / M2")
    i.set_defaults(func=cmd_index)
    l = sub.add_parser("list", help="show the partitions of one OTA (URL or file) with their SHA-256")
    l.add_argument("ota")
    l.add_argument("--json", action="store_true", help="print the whole description as JSON")
    l.set_defaults(func=cmd_list)
    ls = sub.add_parser("listings", help="save every indexed OTA's partition table as <out>/<codename>/<build>.json")
    ls.add_argument("--index", required=True, help="JSON from `index`")
    ls.add_argument("--out", required=True)
    ls.add_argument("--device", action="append")
    ls.add_argument("--limit", type=int, help="only the newest N OTAs not yet listed")
    ls.add_argument("--refresh", action="store_true")
    ls.add_argument("--jobs", type=int, default=4)
    ls.set_defaults(func=cmd_listings)
    e = sub.add_parser("extract", help="write partitions of one OTA to a folder, checked against the manifest")
    e.add_argument("ota")
    e.add_argument("--out", required=True)
    e.add_argument("--partition", action="append",
                   help="partition name (repeatable; default every bootloader and radio partition)")
    e.set_defaults(func=cmd_extract)
    f = sub.add_parser("fetch", help="pull the wanted kinds out of every indexed OTA")
    f.add_argument("--index", required=True, help="JSON from `index`")
    f.add_argument("--out", required=True, help="folder for <codename>/<build>/payload.bin/... and manifest.json")
    f.add_argument("--device", action="append")
    f.add_argument("--kinds", default=",".join(KINDS), help=f"comma-separated: {', '.join(KINDS)} (default all)")
    f.add_argument("--partition", action="append", help="also pull this partition (repeatable)")
    f.add_argument("--no-carriers", action="store_true")
    f.add_argument("--gsc-all", action="store_true", help="also read devices with no known GSC")
    f.add_argument("--limit", type=int, help="only the newest N OTAs")
    f.add_argument("--refresh", action="store_true")
    f.add_argument("--max-size", type=int, default=16 << 20, help="largest GSC file to pull (bytes)")
    f.set_defaults(func=cmd_fetch)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
