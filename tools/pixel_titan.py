#!/usr/bin/env python3
"""Titan M (GSC) firmware repair from the bootloader (Python 3.9+).

Finds the chip's firmware, `<gsc-flavor>.bin` (d3m2.bin on a Titan M2), in a
factory image ZIP, a bootloader/radio FBPK image, a folder or the file itself,
then sends it with `fastboot stage <file>` (into the bootloader's download
buffer), `fastboot oem gsc rescue` (hands the buffer to the chip's rescue
loader) and `fastboot oem gsc reset` (restarts the chip on the new image).

Nothing is sent to the phone without --apply and the serial typed back. Titan
anti-rollback is one-way, so an image whose build or signed-header version is
older than the newest RW image the chip reports needs --allow-downgrade; one
whose rollback level is lower (the chip will not boot it) or that cannot be
read is refused; a higher rollback level needs --allow-rollback-bump. Android
re-flashes the Titan from the vendor image at boot, so a downgrade only lasts
if the vendor image carries the same firmware. The
only `oem gsc` commands this sends are `rescue` and `reset`, plus the
read-only `version`, `header` and `avb state` queries; never `avb rb`.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

_spec = importlib.util.spec_from_file_location("pixel_modem", Path(__file__).with_name("pixel_modem.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)
fail = m.fail

# Build tag Titan firmware carries as text: the `ab` number is the Android
# build it came from, and orders builds; the hash names the source revision.
TAG_RE = re.compile(r"nugget_v\d+\.\d+\.ab(\d+)-([0-9a-f]{7,40})(?:-[a-z]+)?")
# The only state-changing `oem gsc` commands this tool sends.
GSC_WRITES = {("rescue",), ("reset",)}
# Cr50's SignedHeader (chromiumos/platform/ec chip/g/signed_header.h), which
# starts every Titan M image: 1024 bytes, little-endian, magic fffffffd on a
# Titan M2 (Dauntless) and, inferred, fffffffe on a Titan M (Citadel).
HEADER_SIZE = 1024
HEADER_MAGIC = re.compile(rb"[\xfd\xfe]\xff\xff\xff")
HEADER_FIELDS = {"image_size": 808, "ro_base": 812, "ro_max": 816, "epoch": 860,
                 "major": 864, "minor": 868}
INFOMAP = slice(844, 860)


def parse_running(version):
    """{image: {booted, version, tag, build, hash, state}} from what
    `Fastboot.gsc_version()` returns (lines or pixel_modem's parsed dicts);
    other shapes are kept as {"raw": value}."""
    images = {}
    for name, value in version.items() if isinstance(version, dict) else ():
        info = m.parse_gsc_image(value) if isinstance(value, str) else value
        if not isinstance(info, dict) or "image_version" not in info:
            images[name.lower()] = {"raw": value}
            continue
        entry = {"booted": bool(info.get("running")), "version": info["image_version"],
                 "tag": info.get("build"), "state": info.get("state")}
        if info.get("build_id"):
            entry["build"] = int(info["build_id"])
        if info.get("commit"):
            entry["hash"] = info["commit"]
        images[name.lower()] = entry
    return images


def signed_headers(data):
    """Every plausible Titan M SignedHeader in an image, by offset."""
    found = []
    for match in HEADER_MAGIC.finditer(data):
        off = match.start()
        if off % 4 or off + HEADER_SIZE > len(data):
            continue
        h = {k: int.from_bytes(data[off+o:off+o+4], "little") for k, o in HEADER_FIELDS.items()}
        if not (HEADER_SIZE <= h["image_size"] <= len(data) - off and h["ro_base"] < h["ro_max"]
                and max(h["epoch"], h["major"], h["minor"]) < 0x10000):
            continue
        words = data[off+INFOMAP.start:off+INFOMAP.stop]
        infomap = " ".join(f"{int.from_bytes(words[i:i+4], 'little'):08x}" for i in range(0, 16, 4))
        found.append({"offset": off, "ro_base": f"{h['ro_base']:#010x}",
                      "version": (h["epoch"], h["major"], h["minor"]),
                      "rollback_level": len(m._bits(infomap, set_bits=False))})
    return found


def header_check(chip, image, allow_bump=False, allow_downgrade=False):
    """Compare the image's RW headers with the chip's (`oem gsc header`,
    parsed by pixel_modem): refuse a lower epoch.major.minor unless
    allow_downgrade; a lower rollback level always, because the chip will not
    boot it; and a higher one unless allow_bump, because booting it burns INFO
    bits the chip can never get back."""
    chip = {n.lower(): h for n, h in (chip or {}).items() if isinstance(h, dict)}
    rw = {n: h for n, h in chip.items() if n.startswith("rw") and "epoch" in h
          and isinstance(h.get("infomap"), dict)}
    if not rw:
        fail("The Titan M reported no RW header (oem gsc header); without its version and "
             "rollback level there is no floor, so nothing will be staged")
    if not image:
        fail("No Titan M signed header found in the image; refusing")
    ro_bases = {h.get("ro_base") for n, h in chip.items() if n.startswith("ro")}
    floor_version = max((h["epoch"], h["major"], h["minor"]) for h in rw.values())
    floor_level = max(h["infomap"]["rollback_level"] for h in rw.values())
    # A header at an RO image's address is RO; anything else is held to the RW floor.
    checked = [h for h in image if h["ro_base"] not in ro_bases]
    if not checked:
        fail("The image holds only RO headers; rescue writes RW, refusing")
    for h in checked:
        where = (f"header at {h['offset']:#x} ({'.'.join(map(str, h['version']))}, "
                 f"rollback level {h['rollback_level']})")
        if h["version"] < floor_version and not allow_downgrade:
            fail(f"{where} is older than RW {'.'.join(map(str, floor_version))} on the chip; "
                 "pass --allow-downgrade to send it")
        if h["rollback_level"] < floor_level:
            fail(f"{where} is below the chip's rollback level {floor_level}; it would not boot")
        if h["rollback_level"] > floor_level and not allow_bump:
            fail(f"{where} raises the rollback level from {floor_level}, which cannot be undone; "
                 "pass --allow-rollback-bump if that is intended")
    return {"floor": {"version": ".".join(map(str, floor_version)), "rollback_level": floor_level},
            "image_headers": [dict(h, version=".".join(map(str, h["version"]))) for h in image]}


def image_builds(data):
    """Every distinct build tag in a firmware image, oldest first."""
    found = {(int(mt.group(1)), mt.group(2).decode(), mt.group(0).decode())
             for mt in re.finditer(TAG_RE.pattern.encode(), data)}
    return [{"build": b, "hash": h, "tag": t} for b, h, t in sorted(found)]


def rollback_check(running, builds, allow_downgrade=False):
    """Refuse unless every build in the image is at least the newest RW build
    the chip reports, or allow_downgrade. Returns what was compared."""
    rw = {n: r for n, r in running.items() if n.startswith("rw") and "build" in r}
    if not rw:
        fail("The Titan M reported no RW build (oem gsc version); without it there is "
             "no rollback floor, so nothing will be staged")
    floor_name = max(rw, key=lambda n: rw[n]["build"])
    floor = rw[floor_name]["build"]
    if not builds:
        fail("No nugget build tag found in the image; its age cannot be checked, "
             "so it will not be staged")
    oldest = builds[0]
    downgrade = oldest["build"] < floor
    if downgrade and not allow_downgrade:
        fail(f"Image carries build ab{oldest['build']} ({oldest['tag']}), older than "
             f"{floor_name.upper()} ab{floor} on the chip; pass --allow-downgrade to send it")
    booted = next((r for r in rw.values() if r.get("booted")), None)
    return {
        "floor": {"image": floor_name.upper(), "build": floor, "tag": rw[floor_name]["tag"]},
        "image_builds": builds,
        "same_as_booted": bool(booted and any(b["build"] == booted["build"] for b in builds)),
        "downgrade": downgrade,
    }


def ro_matches(running, data):
    """RO images report only a hash; say which of them the image also carries."""
    return {n.upper(): r["hash"].encode() in data for n, r in running.items()
            if n.startswith("ro") and r.get("hash")}


def fbpk_members(data):
    """{name: bytes} for the data entries of an FBPK v2 image; None if not one."""
    if len(data) < 112 or data[:4] != b"FBPK" or int.from_bytes(data[4:8], "little") != 2:
        return None
    members = {}
    for i in range(int.from_bytes(data[104:108], "little")):
        base = 112 + i * 104
        if base + 104 > len(data):
            fail("Truncated FBPK entry table")
        if int.from_bytes(data[base:base+4], "little") == 0:
            continue
        name = data[base+4:base+40].split(b"\0")[0].decode("ascii", "replace")
        offset = int.from_bytes(data[base+80:base+88], "little")
        size = int.from_bytes(data[base+88:base+96], "little")
        if offset + size > len(data):
            fail(f"FBPK entry {name} runs past the end of the image")
        members[name] = data[offset:offset+size]
    return members


def wanted(name, flavor):
    """Is this file or pack entry the firmware for `flavor`? `.rec` is the
    rescue image Pixels with a Titan M ship (citadel ec.rec, sent with `oem
    citadel rescue`); on a Titan M2 `<flavor>.bin` is the RW firmware. An
    `ec.rec` or `ec.bin` is taken only when passed as the file itself."""
    base = Path(name).name.lower()
    return base in (f"{flavor}.bin", f"{flavor}.ec.bin", f"{flavor}.rec", f"{flavor}.ec.rec", flavor)


def rescue_payload(data):
    """The bytes an image holds: Intel HEX (the form UART rescue takes) decoded,
    anything else as it is."""
    if not data.startswith(b":"):
        return data
    chunks, base = {}, 0
    for line in data.split():
        try:
            rec = bytes.fromhex(line[1:].decode("ascii")) if line.startswith(b":") else b""
        except (ValueError, UnicodeDecodeError):
            rec = b""
        if len(rec) < 5 or len(rec) != rec[0] + 5 or sum(rec) & 0xFF:
            fail("Image starts like Intel HEX but has a malformed record")
        count, addr, kind, payload = rec[0], int.from_bytes(rec[1:3], "big"), rec[3], rec[4:-1]
        if kind == 0:
            chunks[base + addr] = payload
        elif kind in (2, 4):
            base = int.from_bytes(payload, "big") << (4 if kind == 2 else 16)
        elif kind == 1:
            break
    if not chunks:
        fail("Intel HEX image holds no data")
    start = min(chunks)
    out = bytearray(max(a + len(d) for a, d in chunks.items()) - start)
    for a, d in chunks.items():
        out[a-start:a-start+len(d)] = d
    return bytes(out)


def find_firmware(path, flavor):
    """(bytes, where it was found, product the package is for or None)."""
    path = Path(path)
    hits = []
    product = None
    if path.is_dir():
        hits = [(p.read_bytes(), str(p)) for p in sorted(path.rglob("*")) if p.is_file() and wanted(p.name, flavor)]
    elif zipfile.is_zipfile(path):
        product = path.name.split("-")[0] or None
        with zipfile.ZipFile(path) as outer:
            for name in outer.namelist():
                base = Path(name).name
                if wanted(base, flavor):
                    hits.append((outer.read(name), f"{path.name}:{name}"))
                elif base.endswith(".img") and base.startswith(("bootloader-", "radio-")):
                    for entry, data in (fbpk_members(outer.read(name)) or {}).items():
                        if wanted(entry, flavor):
                            hits.append((data, f"{path.name}:{base}:{entry}"))
                elif base.startswith("image-") and base.endswith(".zip"):
                    with outer.open(name) as handle, zipfile.ZipFile(handle) as inner:
                        for member in inner.namelist():
                            if wanted(member, flavor):
                                hits.append((inner.read(member), f"{path.name}:{base}:{member}"))
    elif path.is_file():
        data = path.read_bytes()
        members = fbpk_members(data)
        if members is not None:
            hits = [(d, f"{path.name}:{e}") for e, d in members.items() if wanted(e, flavor)]
        elif flavor in path.name.lower() or path.name.lower() in ("ec.rec", "ec.bin"):
            hits = [(data, str(path))]
        else:
            fail(f"{path.name} is not named for this chip's flavor {flavor}")
    else:
        fail(f"{path} does not exist")
    # Rescue writes one RW image, which is what the .rec file is.
    hits = [h for h in hits if h[1].lower().endswith(".rec")] or hits
    if not hits:
        fail(f"No {flavor}.rec or {flavor}.bin in {path}. If it ships inside vendor.img, extract it and "
             f"pass the file itself with --firmware")
    if len({h[0] for h in hits}) > 1:
        fail("Different firmware files match: " + ", ".join(h[1] for h in hits))
    return hits[0][0], hits[0][1], product


def gsc_write(phone, *args):
    """Send one state-changing `oem gsc` command from the allow-list."""
    if tuple(args) not in GSC_WRITES:
        fail(f"Refusing `oem gsc {' '.join(args)}`: not rescue or reset")
    result = subprocess.run([phone.fastboot, "-s", phone.serial, "oem", "gsc", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, check=False)
    text = (result.stderr + result.stdout).decode(errors="replace")
    if result.returncode or "FAILED" in text:
        fail(f"`oem gsc {' '.join(args)}` failed: {text.strip()[-1200:]}")
    return text


def snapshot(phone):
    return {"version": phone.gsc_version(), "headers": phone.gsc_headers(),
            "avb_state": phone.gsc_avb_state()}


def show_running(running):
    for name, r in sorted(running.items()):
        if "raw" in r:
            print(f"  {name.upper()}: {r['raw']}")
        else:
            print(f"  {name.upper()}: {'*' if r['booted'] else ' '} {r['version']}/{r['tag']} {r['state'] or ''}")


def confirm(phone, args):
    if args.confirm is not None:
        answer = args.confirm
    elif sys.stdin.isatty():
        answer = input(f"Type the serial {phone.serial} to stage this firmware on the Titan M: ").strip()
    else:
        fail("Not on a terminal; pass --confirm SERIAL")
    if answer != phone.serial:
        fail("Serial not confirmed; nothing sent")


def repair(args):
    phone = m.Fastboot(args.serial, args.fastboot_bin)
    product = phone.getvar("product")
    flavor = phone.getvar("gsc-flavor")
    if not flavor or not re.fullmatch(r"[a-z0-9]+", flavor):
        fail(f"Bootloader reports no usable gsc-flavor ({flavor!r}); not a Titan M phone in fastboot?")
    data, where, package_product = find_firmware(args.firmware, flavor)
    if package_product and package_product != product:
        fail(f"{Path(args.firmware).name} is for {package_product}; the phone is {product}")
    before = snapshot(phone)
    running = parse_running(before["version"])
    body = rescue_payload(data)
    builds = image_builds(body)
    print(f"{phone.serial}: {product}, {m.titan_model(flavor)} ({flavor}), "
          f"bootloader {phone.getvar('version-bootloader')}")
    print("Titan M firmware now (* = booted):")
    show_running(running)
    print(f"Image: {where} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
    for b in builds:
        print(f"  carries {b['tag']}")
    for name, present in ro_matches(running, body).items():
        print(f"  {name} hash {'present' if present else 'not present'} in the image")
    check = rollback_check(running, builds, args.allow_downgrade)
    check["headers"] = header_check(before["headers"], signed_headers(body), args.allow_rollback_bump,
                                    args.allow_downgrade)
    for h in check["headers"]["image_headers"]:
        print(f"  header at {h['offset']:#x}: base {h['ro_base']}, version {h['version']}, "
              f"rollback level {h['rollback_level']}")
    print(f"Rollback floor {check['floor']['image']} ab{check['floor']['build']}: image is "
          + ("the same build as the booted RW" if check["same_as_booted"] else
             "OLDER (downgrade)" if check["downgrade"] else "newer")
          + f"; header floor {check['headers']['floor']['version']}, "
            f"rollback level {check['headers']['floor']['rollback_level']}")

    out = m.private_dir(args.out)
    image = out / (flavor + (".rec" if where.lower().endswith(".rec") else ".bin"))
    image.write_bytes(data)
    m.write_json(out / "before.json", {"serial": phone.serial, "product": product, "flavor": flavor,
                                       "image": where, "sha256": m.sha(image), "rollback": check,
                                       "gsc": before})
    steps = [["stage", str(image)], ["oem", "gsc", "rescue"], ["oem", "gsc", "reset"]]
    print("Plan:")
    for step in steps:
        print(f"  fastboot -s {phone.serial} {' '.join(step)}")
    if not args.apply:
        print(f"Dry run; phone unchanged. Saved {image} and before.json in {out}. "
              "To send, pass --apply.")
        return
    confirm(phone, args)
    print("Staging Titan M firmware; do not disconnect USB or power.", flush=True)
    phone.call("stage", str(image))
    print(gsc_write(phone, "rescue").strip())
    print(gsc_write(phone, "reset").strip())
    after = snapshot(phone)
    m.write_json(out / "after.json", {"serial": phone.serial, "gsc": after})
    print("Titan M firmware after reset:")
    now = parse_running(after["version"])
    show_running(now)
    bad = [n.upper() for n, r in now.items() if r.get("state") not in (None, "ok")]
    if not now:
        print("The chip did not answer `oem gsc version` yet; run `fastboot oem gsc version` again.")
    elif bad:
        fail("Not ok after reset: " + ", ".join(bad))


def parser():
    p = argparse.ArgumentParser(description=__doc__, prog="pixel_titan.py",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("repair", help="Preview, or with --apply send, stage + rescue + reset of the Titan M firmware")
    rep.add_argument("--firmware", required=True,
                     help="Factory image ZIP, bootloader/radio .img, folder, or <flavor>.bin itself")
    rep.add_argument("--out", required=True, help="New private folder for the image and before/after reports")
    rep.add_argument("--apply", action="store_true", help="Send the commands (asks for the serial)")
    rep.add_argument("--allow-downgrade", action="store_true",
                     help="Allow an RW image older than the chip's (same rollback level only)")
    rep.add_argument("--allow-rollback-bump", action="store_true",
                     help="Allow an image with a higher rollback level than the chip (one-way)")
    rep.add_argument("--confirm", metavar="SERIAL", help="The serial, to confirm --apply without a prompt")
    rep.add_argument("--serial", help="fastboot serial; optional when one device is attached")
    rep.add_argument("--fastboot-bin", default="fastboot", help="fastboot executable")
    rep.set_defaults(func=repair)
    return p


def main():
    os.umask(0o077)
    try:
        args = parser().parse_args()
        args.func(args)
        return 0
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
