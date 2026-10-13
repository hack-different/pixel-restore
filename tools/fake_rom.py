#!/usr/bin/env python3
"""Pretend to be a Tensor boot ROM on a pseudo-terminal so `pixel-restore boot`
can be exercised without a phone.

Speaks the request/clear-to-send/upload dialogue tensor-usbdl documented:
for each stage in the script it sends `eub:req:<serial>:<STAGE>`, then `C`,
reads back one `ESC D N W` frame, checks the length field and payload size,
and moves on. Exits non-zero on any protocol mismatch.

Usage:
    python3 tools/fake_rom.py --image bootloader.img [--serial 09875001...]
Prints the slave pty path on the first line, then waits for the host.
Run the tool against it in another terminal:
    pixel-restore boot --image bootloader.img --port /dev/pts/N --epbl split
"""
import argparse
import os
import pty
import struct
import sys
import time
import tty
import zlib

HEADER = 4096
# The order a Pixel 8 Pro ROM (zuma) asked for stages on real hardware; the
# Pixel 9 ROM is assumed to match. Each tuple is (request name, partition,
# "full" | "header" | "body"). After BL2B the real phone re-enumerates and
# BL2 carries on asking from GSA1; --handoff-gap imitates that pause.
SCRIPT = [
    ("BL1", "bl1", "full"),
    ("DPM", "dpm", "full"),
    ("GCF", "gcf", "header"),
    ("GCFB", "gcf", "body"),
    ("EPBL", "pbl", "header"),
    ("EPBB", "pbl", "body"),
    ("BL2", "bl2", "header"),
    ("BL2B", "bl2", "body"),
    ("GSA1", "gsa_bl1", "full"),
    ("GSAF", "gsa", "full"),
    ("ABL", "abl", "header"),
    ("ABLB", "abl", "body"),
    ("TZSW", "tzsw", "header"),
    ("TZSB", "tzsw", "body"),
    ("LDFW", "ldfw", "header"),
    ("LDFB", "ldfw", "body"),
    ("BL31", "bl31", "header"),
    ("BL3B", "bl31", "body"),
]


def parse_fbpk(data):
    magic, version, hsize, esize = struct.unpack_from("<IIII", data, 0)
    assert magic == 0x4B504246 and version == 2, "not FBPK v2"
    total = struct.unpack_from("<I", data, 104)[0]
    parts = {}
    for i in range(total):
        base = hsize + i * esize
        kind, name, product, off, size, slotted, crc = struct.unpack_from(
            "<I36s40sQQII", data, base
        )
        if kind == 0:
            continue
        name = name.split(b"\0")[0].decode()
        for suffix in ("_a", "_b"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        parts[name] = data[off : off + size]
    return parts


# Header tag (ASCII in the magic word at 0x400) -> the partition it answers.
TAGS = {"APBL": "bl1", "GCF": "gcf", "EPBL": "pbl", "BL2": "bl2", "GSA1": "gsa_bl1",
        "GSAF": "gsa", "ABL": "abl", "TZSW": "tzsw", "LDFW": "ldfw", "BL31": "bl31"}


def load_dir(path):
    """A loose recovery pack (tensor-usbdl sources/<pack>/), keyed by header tag."""
    parts = {}
    for name in os.listdir(path):
        if not name.endswith(".img"):
            continue
        data = open(os.path.join(path, name), "rb").read()
        tag = data[0x400:0x404].rstrip(b"\0").decode("ascii", "replace")
        parts[TAGS.get(tag, name[:-4])] = data
    return parts


def expected_len(parts, partition, part):
    if partition not in parts:
        return HEADER if partition == "dpm" else None
    n = len(parts[partition])
    return {"full": n, "header": HEADER, "body": n - HEADER}[part]


def read_exact(fd, n, timeout=None):
    buf = bytearray()
    # macOS ptys move only a few hundred KB/s; allow for the 8 MB TZSW body.
    deadline = time.time() + (timeout or 30 + n / 50_000)
    while len(buf) < n:
        if time.time() > deadline:
            raise TimeoutError(f"wanted {n} bytes, got {len(buf)}")
        try:
            chunk = os.read(fd, min(65536, n - len(buf)))
        except BlockingIOError:
            time.sleep(0.005)
            continue
        if not chunk:
            time.sleep(0.005)
            continue
        buf += chunk
    return bytes(buf)


def wait_readable(fd, timeout):
    """True once the host has sent something (without consuming it)."""
    import select
    r, _, _ = select.select([fd], [], [], timeout)
    return bool(r)


def drain(fd):
    while True:
        try:
            if not os.read(fd, 65536):
                return
        except BlockingIOError:
            return


def wait_for_host(fd, timeout=60):
    """The host sends '\\n' to wake us; block until something arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if os.read(fd, 4096):
                return
        except BlockingIOError:
            pass
        time.sleep(0.02)
    print("[rom] host never opened the port", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", help="bootloader.img to validate against")
    src.add_argument("--dir", help="recovery pack directory to validate against")
    ap.add_argument("--serial", default="09875001deadbeef0123")
    ap.add_argument("--fail-at", help="pretend this stage's header fails")
    ap.add_argument("--only", type=int, help="stop after N stages")
    ap.add_argument("--handoff-gap", type=float, default=0,
                    help="go silent this many seconds after BL2B, like the real re-enumeration")
    ap.add_argument("--pause-after", help="stage to go silent after (with --pause; default BL2B)")
    ap.add_argument("--pause", type=float, help="seconds of silence after --pause-after")
    args = ap.parse_args()

    parts = load_dir(args.dir) if args.dir else parse_fbpk(open(args.image, "rb").read())
    master, slave = pty.openpty()
    tty.setraw(slave)  # no echo, no line discipline: behave like a USB CDC port
    os.set_blocking(master, False)
    print(os.ttyname(slave), flush=True)

    def say(line):
        os.write(master, (line + "\r\n").encode())

    # Wait for the host to open the port and poke us.
    wait_for_host(master)
    say(f"exynos_usb_booting:eub:{args.serial}")
    sent = 0
    for stage, partition, part in SCRIPT[: args.only]:
        want = expected_len(parts, partition, part)
        if want is None:
            print(f"[rom] {stage}: image has no {partition}, skipping", file=sys.stderr)
            continue
        # Like the real ROM: repeat the request and a bare `C` (no
        # terminator) until the host starts an upload.
        deadline = time.time() + 30
        while True:
            say(f"eub:req:{args.serial}:{stage}")
            os.write(master, b"C")
            if wait_readable(master, 0.8):
                break
            if time.time() > deadline:
                print(f"[rom] {stage}: host never uploaded", file=sys.stderr)
                sys.exit(6)
        hdr = read_exact(master, 8)
        if hdr[:4] != b"\x1bDNW":
            print(f"[rom] bad opcode {hdr[:4]!r} for {stage}", file=sys.stderr)
            sys.exit(2)
        total = struct.unpack("<I", hdr[4:])[0]
        payload = read_exact(master, total - 8 - 2)
        crc = read_exact(master, 2)
        if len(payload) != want:
            print(f"[rom] {stage}: got {len(payload)} bytes, expected {want}", file=sys.stderr)
            sys.exit(3)
        if partition in parts:
            src = parts[partition]
            ref = {"full": src, "header": src[:HEADER], "body": src[HEADER:]}[part]
            if payload != ref:
                print(f"[rom] {stage}: payload bytes differ from image", file=sys.stderr)
                sys.exit(4)
        print(f"[rom] {stage}: ok ({len(payload)} bytes, crc {crc.hex()})", file=sys.stderr)
        sent += 1
        if args.fail_at == stage:
            say(f"{stage.lower()} header fail")
            time.sleep(0.5)
            sys.exit(5)
        say(f"eub:ack:{args.serial}:{stage}")
        gap = args.pause if args.pause is not None else args.handoff_gap
        if stage == (args.pause_after or "BL2B") and gap:
            print(f"[rom] silent for {gap}s after {stage} (re-enumeration)", file=sys.stderr)
            time.sleep(gap)
            drain(master)
    print(f"[rom] handing off to ABL after {sent} stages", file=sys.stderr)
    time.sleep(0.3)
    os.close(master)
    os.close(slave)


if __name__ == "__main__":
    main()
