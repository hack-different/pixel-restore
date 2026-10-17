#!/usr/bin/env python3
"""Tests for pixel_gsc_fetch.py: page parsing, byte-range ZIP reading
(plain, ZIP64 fields, nested stored and deflated), FBPK partitions, and the
fetch command against a local HTTP server that honours Range."""
import http.server
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from pathlib import Path

_spec = importlib.util.spec_from_file_location("pixel_gsc_fetch", Path(__file__).with_name("pixel_gsc_fetch.py"))
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)

PAGE = '''
<h2 id="komodo">"komodo" for Pixel 9 Pro XL</h2>
<table><tr><th>Version</th><th>Flash</th><th>Download</th><th>SHA-256 Checksum</th></tr>
<tr id="komodoap3a.241005.015"><td>15.0.0 (AP3A.241005.015, Oct 2024)</td><td><a href="https://flash.android.com/build/x">Flash</a></td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-ap3a.241005.015-factory-12345678.zip">Link</a></td>
<td>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</td></tr>
<tr id="komodocp3a.260905.009"><td>17.0.0 (CP3A.260905.009, Sep 2026)</td><td><a href="https://flash.android.com/build/y">Flash</a></td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-cp3a.260905.009-factory-99eb621a.zip">Link</a></td>
<td>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</td></tr>
<tr id="komodocp3a.260905.009.v"><td>17.0.0 (CP3A.260905.009.V1, Sep 2026, Verizon)</td><td><a href="https://flash.android.com/build/z">Flash</a></td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-cp3a.260905.009.v1-factory-deadbeef.zip">Link</a></td>
<td>cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc</td></tr>
</table>
<h2 id="newfish">"newfish" for Pixel 11 Mini</h2>
<tr><td>18.0.0 (DP1A.270101.001, Jan 2027)</td><td></td><td><a href="https://dl.google.com/dl/android/aosp/newfish-dp1a.270101.001-factory-00000001.zip">Link</a></td><td>dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd</td></tr>
<h2 id="blueline">"blueline" for Pixel 3</h2>
<tr><td>9.0.0 (PD1A.180720.030, Sep 2018)</td><td></td><td><a href="https://dl.google.com/dl/android/aosp/blueline-pd1a.180720.030-factory-c3f5d0a1.zip">Link</a></td><td>eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee</td></tr>
'''


def fbpk(entries):
    """A synthetic FBPK v2 container, like fbpacktool `create`."""
    out = bytearray(112 + 104 * len(entries))
    out[0:4] = b"FBPK"
    struct.pack_into("<IIII", out, 4, 2, 112, 104, 0)
    struct.pack_into("<I", out, 104, len(entries))
    for i, (name, kind, payload) in enumerate(entries):
        while len(out) % 16:
            out.append(0)
        off = len(out)
        out += payload
        base = 112 + 104 * i
        struct.pack_into("<I", out, base, kind)
        out[base + 4:base + 4 + len(name)] = name.encode()
        struct.pack_into("<QQII", out, base + 80, off, len(payload), 0, zlib.crc32(payload))
    struct.pack_into("<I", out, 108, len(out))
    return bytes(out)


def make_zip(members, stored=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            z.writestr(name, data, compress_type=zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED)
    return buf.getvalue()


D3M2 = b"\xfd\xff\xff\xff" + bytes(range(256)) * 8
REC = b":10000000" + b"A" * 40
import random
_r = random.Random(7)
VENDOR = bytes(_r.getrandbits(8) for _ in range(400000))
INNER = make_zip([("vendor.img", VENDOR), ("firmware/d3m2.bin", D3M2), ("firmware/d3m2.rec", REC)])
BL = fbpk([("abl", 1, b"abl!"), ("d3m2", 1, D3M2), ("partition:0", 0, b"tbl")])


def factory_zip(inner_stored):
    return make_zip([
        ("komodo-cp3a.260905.009/flash-all.sh", b"#!/bin/sh\n"),
        ("komodo-cp3a.260905.009/bootloader-komodo-ripcurrent-1.img", BL),
        ("komodo-cp3a.260905.009/radio-komodo-g5400.img", b"not fbpk"),
        ("komodo-cp3a.260905.009/image-komodo-cp3a.260905.009.zip", INNER),
    ], stored=("komodo-cp3a.260905.009/image-komodo-cp3a.260905.009.zip",) if inner_stored else ())


class RangeHandler(http.server.BaseHTTPRequestHandler):
    files = {}

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        data = self.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()

    def do_GET(self):
        data = self.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        if rng:
            a, b = rng.split("=")[1].split("-")
            a, b = int(a), int(b)
            body = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PageTests(unittest.TestCase):
    def test_parse(self):
        idx = g.parse_page(PAGE)
        self.assertEqual(sorted(idx), ["blueline", "komodo", "newfish"])
        k = idx["komodo"]
        self.assertEqual(k["name"], "Pixel 9 Pro XL")
        self.assertEqual(k["gsc"], "dauntless")
        self.assertEqual([i["build"] for i in k["images"]],
                         ["ap3a.241005.015", "cp3a.260905.009", "cp3a.260905.009.v1"])
        self.assertEqual(k["images"][0]["sha256"], "a" * 64)
        self.assertEqual(k["images"][0]["description"], "15.0.0 (AP3A.241005.015, Oct 2024)")
        self.assertFalse(k["images"][1]["carrier"])
        self.assertTrue(k["images"][2]["carrier"])
        self.assertEqual(k["images"][1]["file"], "komodo-cp3a.260905.009-factory-99eb621a.zip")
        self.assertEqual(idx["newfish"]["name"], "Pixel 11 Mini")
        self.assertEqual(idx["newfish"]["gsc"], "unknown")
        self.assertEqual(idx["blueline"]["gsc"], "citadel")

    def test_index_command(self):
        with tempfile.TemporaryDirectory() as d:
            page = Path(d, "page.html")
            page.write_text(PAGE)
            out = Path(d, "index.json")
            g.main(["index", "--page", str(page), "--out", str(out), "--gsc-only"])
            doc = json.loads(out.read_text())
            self.assertEqual(doc["devices"], 2)
            self.assertEqual(doc["images"], 4)
            self.assertNotIn("newfish", doc["by_codename"])


class ZipTests(unittest.TestCase):
    def test_members_and_extract(self):
        data = factory_zip(True)
        src = g.FileRange.__new__(g.FileRange)
        src.f, src.len, src.bytes_read = io.BytesIO(data), len(data), 0
        members = g.zip_members(src)
        self.assertEqual(len(members), 4)
        bl = next(m for m in members if "bootloader" in m.name)
        self.assertEqual(g.extract(src, bl), BL)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            inner = next(m for m in z.infolist() if m.filename.endswith(".zip"))
            self.assertEqual(inner.compress_type, zipfile.ZIP_STORED)

    def test_fbpk(self):
        self.assertEqual([n for n, _ in g.fbpk_entries(BL)], ["abl", "d3m2"])
        self.assertEqual(dict(g.fbpk_entries(BL))["d3m2"], D3M2)
        self.assertEqual(g.fbpk_entries(b"nope"), [])

    def test_gsc_names(self):
        for n in ("firmware/d3m2.bin", "d2.rec", "vendor/firmware/citadel/ec.bin", "ec.rec",
                  "firmware/dauntless/evt.bin", "x/gsc/d3.ec.bin"):
            self.assertTrue(g.is_gsc(n), n)
        for n in ("vendor.img", "boot.bin", "flash-all.sh", "firmware/modem.bin", "abl.bin"):
            self.assertFalse(g.is_gsc(n), n)

    def walk(self, stored, stream):
        data = factory_zip(stored)
        src = g.FileRange.__new__(g.FileRange)
        src.f, src.len, src.bytes_read = io.BytesIO(data), len(data), 0
        return g.walk_zip(src, "f.zip", stream, 16 << 20)

    def test_walk_stored_nested(self):
        hits, notes = self.walk(True, False)
        self.assertEqual(notes, [])
        wheres = sorted(h.where for h in hits)
        self.assertEqual(wheres, ["f.zip:bootloader-komodo-ripcurrent-1.img:d3m2",
                                  "f.zip:image-komodo-cp3a.260905.009.zip:firmware/d3m2.bin",
                                  "f.zip:image-komodo-cp3a.260905.009.zip:firmware/d3m2.rec"])
        self.assertEqual(len({h.sha256 for h in hits if h.where.endswith(("d3m2", "d3m2.bin"))}), 1)

    def test_walk_deflated_nested(self):
        hits, notes = self.walk(False, False)
        self.assertEqual([h.where for h in hits], ["f.zip:bootloader-komodo-ripcurrent-1.img:d3m2"])
        self.assertEqual(len(notes), 1)
        self.assertIn("--stream-nested", notes[0])
        hits, notes = self.walk(False, True)
        self.assertEqual(len(hits), 3)
        self.assertEqual(notes, [])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RangeHandler.files = {
            "/komodo-cp3a.260905.009-factory-99eb621a.zip": factory_zip(True),
            "/komodo-ap3a.241005.015-factory-12345678.zip": factory_zip(False),
        }
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_http_range_reads_little(self):
        src = g.HttpRange(self.base + "/komodo-cp3a.260905.009-factory-99eb621a.zip")
        hits, notes = g.walk_zip(src, "z", False, 16 << 20)
        self.assertEqual(len(hits), 3)
        # Directory, bootloader, inner directory and the two firmware files: not vendor.img.
        self.assertLess(src.bytes_read, len(VENDOR) // 2)

    def test_fetch_command(self):
        idx = g.parse_page(PAGE)
        for i in idx["komodo"]["images"]:
            i["url"] = self.base + "/" + i["file"]
        with tempfile.TemporaryDirectory() as d:
            index = Path(d, "index.json")
            index.write_text(json.dumps({"by_codename": {"komodo": idx["komodo"]}}))
            out = Path(d, "out")
            g.main(["fetch", "--index", str(index), "--out", str(out), "--backend", "builtin"])
            man = json.loads((out / "manifest.json").read_text())
            self.assertEqual(sorted(man["images"]), ["komodo-ap3a.241005.015-factory-12345678.zip",
                                                     "komodo-cp3a.260905.009-factory-99eb621a.zip"])
            new = man["images"]["komodo-cp3a.260905.009-factory-99eb621a.zip"]
            self.assertEqual(len(new["files"]), 3)
            self.assertTrue((out / "komodo/cp3a.260905.009/image-komodo-cp3a.260905.009.zip/firmware/d3m2.bin").is_file())
            old = man["images"]["komodo-ap3a.241005.015-factory-12345678.zip"]
            self.assertEqual(len(old["files"]), 1)
            self.assertEqual(len(old["notes"]), 1)
            # A second run reads nothing new.
            g.main(["fetch", "--index", str(index), "--out", str(out), "--backend", "builtin"])
            # Carrier builds are skipped by default.
            self.assertNotIn("komodo-cp3a.260905.009.v1-factory-deadbeef.zip", man["images"])


if __name__ == "__main__":
    unittest.main()
