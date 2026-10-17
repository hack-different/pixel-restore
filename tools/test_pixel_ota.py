#!/usr/bin/env python3
"""Tests for pixel_ota.py against hand-built A/B OTA packages: payload
manifest parsing, lazy per-operation partition reads, SHA-256 verification,
GSC files read out of an ext4 vendor partition (skipped without mke2fs), the
OTA listing page, and `fetch` over a local HTTP server that honours Range,
with its manifest read back by `pixel_gsc_fetch.py report`."""
import bz2
import hashlib
import http.server
import importlib.util
import io
import json
import lzma
import random
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location("pixel_ota", Path(__file__).with_name("pixel_ota.py"))
o = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(o)
_spec = importlib.util.spec_from_file_location("test_pixel_gsc_fetch", Path(__file__).with_name("test_pixel_gsc_fetch.py"))
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)

BS = 4096
_r = random.Random(7)


def rand(n):
    return bytes(_r.getrandbits(8) for _ in range(n))


# ------------------------------------------------------------ protobuf writer

def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def fv(field, n):
    return varint(field << 3) + varint(n)


def fb(field, data):
    return varint(field << 3 | 2) + varint(len(data)) + data


def extent(start, count):
    return fv(1, start) + fv(2, count)


class Builder:
    """A payload.bin from {name: (image bytes, [(op type, [(start, count)])])}."""

    def __init__(self):
        self.blobs, self.parts = bytearray(), []

    def add(self, name, image, ops, sha=None, version=None):
        msg = fb(1, name.encode())
        for typ, dst in ops:
            op = fv(1, typ)
            if typ not in o.ZERO_OPS:
                raw = b"".join(image[s * BS:(s + c) * BS] for s, c in dst)
                blob = {0: lambda d: d, 1: bz2.compress, 8: lzma.compress}.get(typ, lambda d: d)(raw)
                op += fv(2, len(self.blobs)) + fv(3, len(blob)) + fb(8, hashlib.sha256(blob).digest())
                self.blobs += blob
            for s, c in dst:
                op += fb(6, extent(s, c))
            msg += fb(8, op)
        info = fv(1, len(image)) + fb(2, bytes.fromhex(sha or hashlib.sha256(image).hexdigest()))
        msg += fb(7, info)
        if version:
            msg += fb(17, version.encode())
        self.parts.append(msg)

    def payload(self, sig=b"S" * 11):
        manifest = fv(3, BS) + fv(12, 0) + b"".join(fb(13, p) for p in self.parts) + fv(14, 1760000000) \
            + fb(18, b"2026-09-05")
        return o.PAYLOAD_MAGIC + struct.pack(">QQI", 2, len(manifest), len(sig)) + manifest + sig + bytes(self.blobs)


def ota_zip(payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/com/android/metadata", "ota-type=AB\npost-build=google/komodo/komodo:17/CP3A\n",
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("payload_properties.txt", "FILE_HASH=abc\nFILE_SIZE=123\n", compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("payload.bin", payload, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


GSC = b"\xfd\xff\xff\xff" + b"nugget_v1.2.3.ab12345678-0123abc-rel " + rand(60000)


def vendor_image():
    """An ext4 vendor image with GSC firmware under /firmware and a large
    incompressible file elsewhere, or None when mke2fs is not installed."""
    mke2fs = shutil.which("mke2fs") or shutil.which("mkfs.ext4")
    if not mke2fs:
        return None
    with tempfile.TemporaryDirectory() as d:
        root = Path(d, "root")
        for name, data in {"firmware/dauntless/d3m2.ec.bin": GSC, "firmware/readme.txt": b"hi",
                           "lib/big.so": rand(3 << 20)}.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        img = Path(d, "vendor.img")
        subprocess.run([mke2fs, "-q", "-t", "ext4", "-b", str(BS), "-d", str(root), str(img), "8M"],
                       check=True, capture_output=True)
        return img.read_bytes()


ABL = rand(2 * BS)
MODEM = rand(2 * BS) + bytes(BS) + rand(BS) + rand(BS) + bytes(BS) + rand(BS)
SYSTEM = rand(BS)
VENDOR = vendor_image()


def sample_payload(vendor=True, abl_sha=None, extra=()):
    b = Builder()
    b.add("abl", ABL, [(8, [(0, 2)])], sha=abl_sha, version="abl-17")
    # bzip2, ZERO, raw, and one raw op writing two separate extents (4 and 6)
    b.add("modem", MODEM, [(1, [(0, 2)]), (6, [(2, 1)]), (0, [(3, 1)]), (0, [(4, 1), (6, 1)]), (7, [(5, 1)])])
    b.add("system", SYSTEM, [(0, [(0, 1)])])
    if vendor and VENDOR:
        n = len(VENDOR) // BS
        b.add("vendor", VENDOR, [(8, [(s, min(16, n - s))]) for s in range(0, n, 16)])
    for name, image, ops in extra:
        b.add(name, image, ops)
    return b.payload()


class Counting(o.afs.Range):
    def __init__(self, data):
        self.data, self.len, self.bytes_read, self.calls = data, len(data), 0, 0

    def read(self, start, end):
        self.bytes_read += end - start
        self.calls += 1
        return self.data[start:end]


class PayloadTests(unittest.TestCase):
    def test_manifest(self):
        p = o.Payload(Counting(sample_payload(vendor=False)))
        self.assertEqual([x.name for x in p.partitions], ["abl", "modem", "system"])
        self.assertEqual(p.block_size, BS)
        self.assertEqual(p.security_patch_level, "2026-09-05")
        self.assertTrue(p.full)
        abl = p.partition("abl")
        self.assertEqual(abl.size, len(ABL))
        self.assertEqual(abl.sha256, hashlib.sha256(ABL).hexdigest())
        self.assertEqual(abl.version, "abl-17")
        d = p.info()["partitions"][1]
        self.assertEqual(d["operations"], {"REPLACE_BZ": 1, "ZERO": 1, "REPLACE": 2, "DISCARD": 1})

    def test_ranges_match_image(self):
        p = o.Payload(Counting(sample_payload(vendor=False)))
        rng = p.open("modem")
        self.assertEqual(rng.read(0, rng.len), MODEM)
        for _ in range(200):
            a = _r.randrange(0, len(MODEM))
            b = _r.randrange(a, len(MODEM) + 1)
            self.assertEqual(rng.read(a, b), MODEM[a:b], (a, b))

    def test_dump_verifies(self):
        p = o.Payload(Counting(sample_payload(vendor=False)))
        buf = io.BytesIO()
        size, digest, ok = o.dump_partition(p, "modem", buf)
        self.assertEqual((size, digest, ok), (len(MODEM), hashlib.sha256(MODEM).hexdigest(), True))
        self.assertEqual(buf.getvalue(), MODEM)
        bad = o.Payload(Counting(sample_payload(vendor=False, abl_sha="00" * 32)))
        with self.assertRaisesRegex(o.afs.Error, "does not match"):
            o.dump_partition(bad, "abl", io.BytesIO())

    def test_incremental_refused(self):
        b = Builder()
        b.add("abl", ABL, [(4, [(0, 2)])])
        p = o.Payload(Counting(b.payload()))
        self.assertFalse(p.full)
        with self.assertRaisesRegex(o.afs.Error, "incremental"):
            p.open("abl").read(0, 10)

    def test_bad_magic(self):
        with self.assertRaisesRegex(ValueError, "CrAU"):
            o.Payload(Counting(b"PK" + bytes(40)))

    def test_kinds(self):
        self.assertEqual(o.partition_kind("abl", o.KINDS), "bootloader")
        self.assertEqual(o.partition_kind("gsa_bl1", o.KINDS), "bootloader")
        self.assertEqual(o.partition_kind("modem", o.KINDS), "radio")
        self.assertIsNone(o.partition_kind("vendor", o.KINDS))
        self.assertIsNone(o.partition_kind("abl", ("radio",)))


class OtaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "komodo-ota-cp3a.260905.009-1234abcd.zip")
        self.path.write_bytes(ota_zip(sample_payload()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_properties(self):
        ota = o.Ota(str(self.path))
        props = ota.properties()
        self.assertEqual(props["FILE_HASH"], "abc")
        self.assertEqual(props["ota-type"], "AB")

    def test_walk(self):
        ota = o.Ota(str(self.path))
        hits, notes = o.walk_ota(ota, o.KINDS)
        got = {h.where.split(":", 2)[2]: h for h in hits}
        self.assertEqual(got["abl.img"].kind, "bootloader")
        self.assertEqual(got["abl.img"].data, ABL)
        self.assertTrue(got["abl.img"].extra["verified"])
        self.assertEqual(got["modem.img"].kind, "radio")
        self.assertNotIn("system.img", got)
        self.assertNotIn("vendor.img", got)
        if VENDOR:
            g = got["vendor/firmware/dauntless/d3m2.ec.bin"]
            self.assertEqual((g.kind, g.data, g.sha256), ("gsc", GSC, hashlib.sha256(GSC).hexdigest()))
            self.assertNotIn("vendor/firmware/readme.txt", got)

    @unittest.skipUnless(VENDOR, "mke2fs not installed")
    def test_gsc_reads_little(self):
        ota = o.Ota(str(self.path))
        hits, _ = o.walk_ota(ota, ("gsc",))
        self.assertEqual([h.data for h in hits], [GSC])
        # big.so (3 MiB of random data) is never fetched
        self.assertLess(ota.bytes_read, 1 << 20)

    def test_named_partition(self):
        hits, _ = o.walk_ota(o.Ota(str(self.path)), ("radio",), partitions=("system",))
        self.assertEqual(sorted((h.kind, h.extra["partition"]) for h in hits),
                         [("partition", "system"), ("radio", "modem")])

    def test_deflated_payload(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("payload.bin", sample_payload(vendor=False), compress_type=zipfile.ZIP_DEFLATED)
        self.path.write_bytes(buf.getvalue())
        hits, _ = o.walk_ota(o.Ota(str(self.path)), ("bootloader",))
        self.assertEqual([h.data for h in hits], [ABL])

    def test_extract_command(self):
        out = Path(self.tmp.name, "out")
        o.main(["extract", str(self.path), "--out", str(out), "--partition", "modem"])
        self.assertEqual((out / "modem.img").read_bytes(), MODEM)


PAGE = '''
<h2 id="komodo">"komodo" for Pixel 9 Pro XL</h2>
<table><tr><th>Version</th><th>Download</th><th>SHA-256 Checksum</th></tr>
<tr><td>17.0.0 (CP3A.260905.009, Sep 2026)</td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-ota-cp3a.260905.009-1234abcd.zip">Link</a></td>
<td>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</td></tr>
<tr><td>15.0.0 (AP3A.241005.015, Oct 2024)</td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-ota-ap3a.241005.015-5678abcd.zip">Link</a></td>
<td>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</td></tr>
<tr><td>17.0.0 (CP3A.260905.009.V1, Sep 2026, Verizon)</td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-ota-cp3a.260905.009.v1-9999abcd.zip">Link</a></td>
<td>cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc</td></tr>
</table>
<a href="https://dl.google.com/dl/android/aosp/komodo-cp3a.260905.009-factory-99eb621a.zip">factory</a>
'''


class PageTests(unittest.TestCase):
    def test_parse(self):
        idx = o.parse_ota_page(PAGE)
        self.assertEqual(list(idx), ["komodo"])
        imgs = idx["komodo"]["images"]
        self.assertEqual([i["build"] for i in imgs], ["ap3a.241005.015", "cp3a.260905.009", "cp3a.260905.009.v1"])
        self.assertEqual(imgs[1]["sha256"], "a" * 64)
        self.assertEqual(imgs[1]["type"], "ota")
        self.assertTrue(imgs[2]["carrier"])
        self.assertFalse(imgs[1]["carrier"])
        self.assertEqual(idx["komodo"]["gsc"], "dauntless")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ota = ota_zip(sample_payload())
        tg.RangeHandler.files = {"/komodo-ota-cp3a.260905.009-1234abcd.zip": cls.ota}
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), tg.RangeHandler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_list_reads_manifest_only(self):
        ota = o.Ota(f"{self.base}/komodo-ota-cp3a.260905.009-1234abcd.zip")
        d = o.describe(ota)
        self.assertEqual(d["partitions"][0]["sha256"], hashlib.sha256(ABL).hexdigest())
        # the ZIP tail (64 KiB), the local header and the manifest, not the blobs
        self.assertLess(ota.bytes_read, 100000)

    def test_fetch_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            index = {"by_codename": {"komodo": {"name": "Pixel 9 Pro XL", "gsc": "dauntless", "images": [
                {"build": "cp3a.260905.009", "url": f"{self.base}/komodo-ota-cp3a.260905.009-1234abcd.zip",
                 "sha256": None, "description": "17.0.0 (CP3A.260905.009, Sep 2026)", "carrier": False,
                 "file": "komodo-ota-cp3a.260905.009-1234abcd.zip", "type": "ota"},
                {"build": "cp3a.260905.010", "url": f"{self.base}/missing.zip", "sha256": None,
                 "description": "17.0.0 (CP3A.260905.010, Sep 2026)", "carrier": False,
                 "file": "missing.zip", "type": "ota"}]}}}
            Path(d, "index.json").write_text(json.dumps(index))
            out = Path(d, "out")
            o.main(["fetch", "--index", str(Path(d, "index.json")), "--out", str(out)])
            manifest = json.loads((out / "manifest.json").read_text())
            entry = manifest["images"]["komodo-ota-cp3a.260905.009-1234abcd.zip"]
            files = {f["path"]: f for f in entry["files"]}
            abl = files["komodo/cp3a.260905.009/payload.bin/abl.img"]
            self.assertEqual(abl["sha256"], hashlib.sha256(ABL).hexdigest())
            self.assertEqual(abl["manifest_sha256"], abl["sha256"])
            self.assertEqual((out / abl["path"]).read_bytes(), ABL)
            self.assertIn("error", manifest["images"]["missing.zip"])
            self.assertTrue((out / "komodo" / "cp3a.260905.009.ota.json").is_file())
            if VENDOR:
                gsc = files["komodo/cp3a.260905.009/payload.bin/vendor/firmware/dauntless/d3m2.ec.bin"]
                self.assertEqual(gsc["tag"], "nugget_v1.2.3.ab12345678-0123abc-rel")
            # the factory tool's report reads the same manifest
            tg.g.main(["report", "--out", str(out)])
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["distinct_blobs"], len(files))


if __name__ == "__main__":
    unittest.main()
