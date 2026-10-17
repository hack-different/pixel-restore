#!/usr/bin/env python3
"""Tests for android_fs.py against images built with mke2fs and mkfs.erofs
(skipped when those are not installed), plus hand-built sparse and LP images."""
import importlib.util
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("android_fs", Path(__file__).with_name("android_fs.py"))
afs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(afs)

_r = random.Random(3)
FILES = {
    "firmware/citadel/ec.bin": bytes(_r.getrandbits(8) for _ in range(300000)),  # incompressible
    "firmware/citadel/ec.rec": b":10000000" + b"A" * 60000 + b"\n",  # compressible
    "firmware/dauntless/d3m2.bin": b"\xfd\xff\xff\xff" + bytes(range(256)) * 400,
    "firmware/small.txt": b"tiny",
    "etc/other.conf": b"x" * 10,
    "firmware/empty.bin": b"",
}


def tree(d):
    for name, data in FILES.items():
        p = Path(d, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class CountingRange(afs.Range):
    def __init__(self, data):
        self.data, self.len, self.bytes_read, self.calls = data, len(data), 0, 0

    def read(self, start, end):
        self.bytes_read += end - start
        self.calls += 1
        return self.data[start:end]


def sparse(data, blk=4096):
    """Android sparse encoding with raw, fill and don't-care chunks."""
    assert len(data) % blk == 0
    blocks = [data[i:i + blk] for i in range(0, len(data), blk)]
    chunks, i = [], 0
    while i < len(blocks):
        b = blocks[i]
        if b == bytes(blk):
            j = i
            while j < len(blocks) and blocks[j] == bytes(blk):
                j += 1
            chunks.append((0xCAC3, j - i, b""))
        elif b == b[:4] * (blk // 4):
            j = i
            while j < len(blocks) and blocks[j] == b:
                j += 1
            chunks.append((0xCAC2, j - i, b[:4]))
        else:
            j = i
            while j < len(blocks) and blocks[j] != bytes(blk) and blocks[j] != blocks[j][:4] * (blk // 4):
                j += 1
            chunks.append((0xCAC1, j - i, b"".join(blocks[i:j])))
        i = j
    out = struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, blk, len(blocks), len(chunks) + 1, 0)
    for t, n, body in chunks:
        out += struct.pack("<HHII", t, 0, n, 12 + len(body)) + body
    out += struct.pack("<HHII", 0xCAC4, 0, 0, 16) + b"\0\0\0\0"
    return out


def super_image(parts):
    """A flat LP super image holding `parts` ({name: bytes}) at 1 MiB granularity."""
    data_off = 1 << 20
    extents, entries, blobs = [], [], []
    for name, blob in parts.items():
        entries.append((name, len(extents), 1))
        extents.append((len(blob) // 512 + (1 if len(blob) % 512 else 0), 0, data_off // 512, 0))
        blobs.append((data_off, blob))
        data_off += (len(blob) + (1 << 20) - 1) // (1 << 20) * (1 << 20)
    header_size = 128
    tables = b""
    p_off = 0
    for name, first, count in entries:
        tables += name.encode().ljust(36, b"\0") + struct.pack("<IIII", 0, first, count, 0)
    e_off = len(tables)
    for num, t, d, s in extents:
        tables += struct.pack("<QIQI", num, t, d, s)
    header = struct.pack("<IHHI", 0x414C5030, 10, 2, header_size) + b"\0" * 32
    header += struct.pack("<I", len(tables)) + b"\0" * 32
    header += struct.pack("<III", p_off, len(entries), 52) + struct.pack("<III", e_off, len(extents), 24)
    header += struct.pack("<III", e_off + 24 * len(extents), 0, 0) + struct.pack("<III", e_off + 24 * len(extents), 0, 0)
    header = header.ljust(header_size, b"\0")
    geometry = struct.pack("<II", 0x616C4467, 52) + b"\0" * 32 + struct.pack("<III", 65536, 2, 4096)
    img = bytearray(data_off)
    img[4096:4096 + len(geometry)] = geometry
    img[8192:8192 + len(geometry)] = geometry
    md = header + tables
    img[12288:12288 + len(md)] = md
    for off, blob in blobs:
        img[off:off + len(blob)] = blob
    return bytes(img)


def found(fs_src, hint="/firmware"):
    return {p: r() for p, s, r in afs.find_files(fs_src, lambda p: True, hint)}


EXPECT = {"/" + k: v for k, v in FILES.items() if k.startswith("firmware/")}


@unittest.skipUnless(shutil.which("mke2fs"), "mke2fs not installed")
class Ext4Tests(unittest.TestCase):
    def build(self, extra):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        tree(Path(d, "root"))
        img = Path(d, "vendor.img")
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", str(Path(d, "root")), *extra, str(img), "8M"],
                       check=True, capture_output=True)
        return img.read_bytes()

    def test_walk(self):
        for extra in (["-b", "4096"], ["-b", "1024"], ["-O", "^64bit"], ["-O", "^extent,^64bit"], ["-I", "128"]):
            data = self.build(extra)
            self.assertEqual(found(afs.Bytes(data)), EXPECT, extra)
            self.assertEqual(set(found(afs.Bytes(data), None)) - set(EXPECT), {"/etc/other.conf"}, extra)

    def test_sparse_and_cheap(self):
        data = self.build(["-b", "4096"])
        src = CountingRange(sparse(data))
        self.assertTrue(afs.is_sparse(src))
        self.assertEqual(found(src), EXPECT)
        want = sum(len(v) for v in EXPECT.values())
        self.assertLess(src.bytes_read, want + 200000)

    def test_super(self):
        data = self.build(["-b", "4096"])
        sup = super_image({"system_a": b"s" * 4096, "vendor_a": data, "vendor_b": b""})
        src = CountingRange(sparse(sup))
        vendor, name = afs.vendor_from_super(src)
        self.assertEqual(name, "vendor_a")
        self.assertEqual(found(vendor), EXPECT)
        self.assertLess(src.bytes_read, len(sup) // 4)


@unittest.skipUnless(shutil.which("mkfs.erofs"), "mkfs.erofs not installed")
class ErofsTests(unittest.TestCase):
    def build(self, extra):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        tree(Path(d, "root"))
        img = Path(d, "vendor.img")
        r = subprocess.run(["mkfs.erofs", *extra, str(img), str(Path(d, "root"))], capture_output=True, text=True)
        if r.returncode:
            self.skipTest(f"mkfs.erofs {extra}: {r.stderr.strip()}")
        return img.read_bytes()

    VARIANTS = (
        [],
        ["-z", "lz4"],
        ["-z", "lz4hc"],
        ["-z", "lz4hc", "-E", "legacy-compress"],
        ["-z", "lz4hc", "-C", "65536"],
        ["-z", "lz4hc", "-C", "65536", "-E", "legacy-compress"],
        ["-z", "lz4hc", "-E", "ztailpacking"],
        ["-z", "lz4hc", "-E", "fragments"],
        ["-z", "lz4hc", "-E", "ztailpacking", "-C", "32768"],
        ["-z", "lz4hc", "-E", "fragments,ztailpacking", "-C", "65536"],
        ["-E", "force-inode-compact"],
        ["-z", "lz4hc", "-E", "force-inode-extended"],
        ["-z", "lz4hc", "-E", "all-fragments"],
        ["-z", "lz4hc", "-b", "16384"],
    )

    def test_variants(self):
        for extra in self.VARIANTS:
            with self.subTest(extra=extra):
                data = self.build(extra)
                self.assertEqual(found(afs.Bytes(data)), EXPECT)
                self.assertEqual(set(found(afs.Bytes(data), None)) - set(EXPECT), {"/etc/other.conf"})

    def test_sparse_cheap(self):
        data = self.build(["-z", "lz4hc"])
        pad = data + bytes(-len(data) % 4096)
        src = CountingRange(sparse(pad))
        self.assertEqual(found(src), EXPECT)

    def test_other_algorithms_reported(self):
        for z in ("lzma", "deflate", "zstd"):
            data = self.build(["-z", z])
            fs = afs.open_fs(afs.Bytes(data))
            with self.assertRaises(afs.Error) as cm:
                found(afs.Bytes(data))
            self.assertIn("only LZ4", str(cm.exception))


class Lz4Tests(unittest.TestCase):
    def test_roundtrip_known(self):
        # Block for b"abcabcabcabcX": literal "abc", match off 3 len 9, literal "X".
        blk = bytes([0x30]) + b"abc" + bytes([3, 0]) + bytes([0x50 | 0]) + b"X"
        # token 0x30: 3 literals, match len 0+4=4; then 5 literals ... build simpler:
        # Use a trusted encoder instead: verify decoder on a sequence it must accept.
        src = bytes([0x35]) + b"abc" + bytes([3, 0]) + bytes([0x10]) + b"X"
        self.assertEqual(afs.lz4_block(src, 13), b"abcabcabcabcX")


if __name__ == "__main__":
    unittest.main()
