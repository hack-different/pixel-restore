#!/usr/bin/env python3
"""Pixel modem diagnostics and original-IMEI repair preparation (Python 3.9+).

Independent implementation; upstream sources are fetched at pinned revisions for
review, never imported or executed. All device commands select an explicit serial.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

SOURCES = {
    "restore-imei": {
        "url": "https://github.com/bitdomo/restore_imei.git",
        "commit": "2132f425d3dcc6baa2e06d151f316f6e2ec8c4d5",
        "purpose": "Original IMEI restoration; author tested Pixel 9 Pro XL and Pixel 7.",
        "caution": "No LICENSE found. Linux EFS script overwrites cpsha and efs_backup; README differs from code. Do not run unchanged.",
    },
    "pixel-kit": {
        "url": "https://github.com/not-GIANT/Pixel-Kit.git",
        "commit": "6adb667041cf9207e2ad7340ed5f1b79e5d6372b",
        "purpose": "Windows GUI with diagnostics, partition backup and IMEI repair.",
        "caution": "No LICENSE found. Android 17 unverified; CPID path invokes lexipwn and mutates modem identity. Review only.",
    },
    "shannon": {
        "url": "https://github.com/davwheat/shannon-pixel-modem-nvitem-enabler-scripts.git",
        "commit": "e1d1751a1f74ec9b683d39b611cb6720a8f4d8b6",
        "purpose": "Shannon modem NV command documentation and device-specific feature scripts.",
        "caution": "GPL-3.0. Pixel 6/7 examples are not Pixel 9 repair scripts; no generic EFS reset.",
    },
}
PARTITIONS = ("devinfo", "efs", "efs_backup", "persist")
PROPERTIES = {
    "model": "ro.product.device", "manufacturer": "ro.product.manufacturer",
    "android": "ro.build.version.release", "fingerprint": "ro.build.fingerprint",
    "bootloader_locked": "ro.boot.flash.locked", "verified_boot": "ro.boot.verifiedbootstate",
    "baseband": "gsm.version.baseband",
}
MAX_PARTITION = 1024 * 1024 * 1024


def fail(message):
    raise ValueError(message)


def run(argv, timeout=30, stdout=None):
    try:
        result = subprocess.run(argv, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Command timed out; operation may be incomplete: " + argv[0]) from exc
    if result.returncode:
        error = result.stderr.decode(errors="replace").strip()[-1200:]
        fail(f"{argv[0]} failed ({result.returncode}): {error}")
    return result.stdout or b""


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def valid_imei(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{15}", value):
        return False
    if len(set(value)) == 1:
        return False
    digits = list(map(int, value))
    return sum(d if i % 2 == 0 else (2*d)//10 + (2*d)%10
               for i, d in enumerate(digits)) % 10 == 0


def imei_offsets(data):
    """Recognize named DIUS/DIFR TLV records; never guess from digit runs.

    Layout documented in Pixel-Kit's resources/cpid_logic.py. Require DEVI,
    bounded records, unique keys and exactly 16-byte NUL-terminated values.
    Unknown layouts are rejected, including unnamed legacy representations.
    """
    if len(data) < 128 or data[:4] != b"DEVI":
        fail("Unrecognized devinfo header; expected DEVI and a 128-byte header")
    offsets = {}
    pos = 128
    while pos < len(data):
        if data[pos:pos+4] not in (b"DIUS", b"DIFR"):
            pos += 1
            continue
        if pos + 12 > len(data):
            fail("Truncated devinfo record header")
        length = int.from_bytes(data[pos+4:pos+8], "little")
        key_length = int.from_bytes(data[pos+8:pos+12], "little")
        end = pos + 12 + length
        if length == 0 or end > len(data) or not 0 < key_length <= length:
            fail("Malformed devinfo record; refusing to infer offsets")
        key = data[pos+12:pos+12+key_length].rstrip(b"\0")
        offset = pos + 12 + key_length
        if key in (b"imei1", b"imei2"):
            name = key.decode()
            if name in offsets:
                fail("Duplicate IMEI record: " + name)
            if end - offset != 16 or data[end-1] != 0:
                fail("Unexpected IMEI field size or terminator: " + name)
            offsets[name] = offset
        pos = end
    if set(offsets) != {"imei1", "imei2"}:
        fail("Missing named IMEI records; no heuristic fallback is permitted")
    return offsets


def patch_devinfo(data, imeis):
    if set(imeis) != {"imei1", "imei2"} or not all(valid_imei(v) for v in imeis.values()):
        fail("Supply both original 15-digit IMEIs with valid Luhn checksums")
    if imeis["imei1"] == imeis["imei2"]:
        fail("IMEI1 and IMEI2 must be distinct")
    offsets = imei_offsets(data)
    result = bytearray(data)
    for key, offset in offsets.items():
        result[offset:offset+15] = imeis[key].encode("ascii")
    return bytes(result), offsets


class Device:
    def __init__(self, serial, adb="adb"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
            fail("Invalid serial")
        self.serial, self.adb = serial, adb
        if self.call("get-state").strip() != b"device":
            fail("Selected device is not online and authorized")

    def call(self, *args, timeout=30, stdout=None):
        return run([self.adb, "-s", self.serial, *args], timeout=timeout, stdout=stdout)

    def root(self, command, timeout=30, stdout=None):
        # adb joins its shell arguments: quote the entire su command once.
        return self.call("exec-out", "su", "-c", shlex.quote(command),
                         timeout=timeout, stdout=stdout)

    def info(self):
        result = {"serial": self.serial}
        for name, prop in PROPERTIES.items():
            result[name] = self.call("shell", "getprop", prop).decode().strip()
        return result

    def require_root(self):
        if self.root("id -u").strip() != b"0":
            fail("Root shell access is required; this tool does not root/unlock the phone")

    def size(self, partition):
        value = self.root("blockdev --getsize64 /dev/block/by-name/" + partition).strip()
        if not value.isdigit() or not 0 < int(value) <= MAX_PARTITION:
            fail("Invalid or excessive partition size: " + partition)
        return int(value)

    def digest(self, partition):
        output = self.root("sha256sum /dev/block/by-name/" + partition, timeout=180)
        digest = output.decode().split()[0] if output.strip() else ""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            fail("Invalid device hash for " + partition)
        return digest


def source_fetch(args):
    spec = SOURCES[args.name]
    dest = Path(args.out).resolve()
    if dest.exists():
        fail("Source destination must not already exist")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Pin commit, disable checkout hooks, and never initialize submodules.
    prefix = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false"]
    run(prefix + ["init", str(dest)])
    prefix += ["-C", str(dest)]
    run(prefix + ["remote", "add", "origin", spec["url"]])
    run(prefix + ["fetch", "--depth=1", "origin", spec["commit"]], timeout=180)
    run(prefix + ["checkout", "--detach", spec["commit"]])
    actual = run(prefix + ["rev-parse", "HEAD"]).decode().strip()
    if actual != spec["commit"]:
        fail("Fetched revision does not match pinned commit")
    print(f"Fetched {args.name} at {actual} into {dest}; nothing executed.")
    print(spec["caution"])


def summarize_logs(text):
    # No raw log persistence: logs can contain accounts, tokens and device IDs.
    bad = re.findall(r'invalid hardware identifier: "([^"\n]*)" is not a valid IMEI', text)
    return {
        "invalid_imei_checkin": bool(bad),
        "invalid_identifier_lengths": sorted({len(v) for v in bad}),
        "authentication_failed": "AUTHENTICATION_FAILED" in text,
        "checkin_failed": "Checkin failed" in text,
        "key_attestation_error": "KeyStoreException" in text,
    }


def diagnose(args):
    device = Device(args.serial, args.adb)
    report = device.info()
    package = device.call("shell", "pm", "list", "packages", "-U", "com.google.android.gms").decode()
    match = re.search(r"package:com\.google\.android\.gms uid:(\d+)\b", package)
    if not match:
        fail("Could not identify Google Play services UID")
    log = device.call("logcat", "-d", "-t", "10000", "--uid=" + match[1], "-v", "brief").decode(errors="replace")
    report["sign_in"] = summarize_logs(log)
    report["root_binary_present"] = bool(device.call("shell", "sh", "-c", shlex.quote("command -v su || true")).strip())
    report["compatibility"] = "devinfo workflow limited to komodo; Android 17 unverified"
    if args.out:
        write_json(args.out, report)
    print(json.dumps(report, indent=2))


def backup(args):
    device = Device(args.serial, args.adb)
    info = device.info()
    if info["manufacturer"].lower() != "google":
        fail("Backup workflow expects a Google Pixel")
    device.require_root()
    out = private_dir(args.out)
    manifest = {"schema": 1, "device": info, "created": datetime.now(timezone.utc).isoformat(), "files": {}}
    for partition in PARTITIONS:
        path = out / (partition + ".img")
        size = device.size(partition)
        before = device.digest(partition)
        with path.open("xb") as handle:
            device.root("dd if=/dev/block/by-name/" + partition + " bs=1048576", timeout=180, stdout=handle)
        if path.stat().st_size != size or sha(path) != before or device.digest(partition) != before:
            fail("Backup verification failed (partition may be changing): " + partition)
        manifest["files"][path.name] = {"size": size, "sha256": before}
        print("Verified backup: " + partition)
    # persist already contains this file; retain a separate readable copy too.
    cpsha = device.root("cat /mnt/vendor/persist/modem/cpsha")
    if not cpsha.strip():
        fail("Empty cpsha; cannot mark backup complete")
    (out / "cpsha.bin").write_bytes(cpsha)
    manifest["files"]["cpsha.bin"] = {"size": len(cpsha), "sha256": sha(out / "cpsha.bin")}
    # Manifest is the completion marker, written only after every read verifies.
    write_json(out / "manifest.json", manifest)
    print(f"Complete backup: {out}. Contains device identity material; keep private.")


def check_backup(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text())
    required = {p + ".img" for p in PARTITIONS} | {"cpsha.bin"}
    if manifest.get("schema") != 1 or set(manifest.get("files", {})) != required:
        fail("Incomplete or unsupported backup manifest")
    for name in sorted(required):
        path = folder / name
        spec = manifest["files"][name]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != spec["size"] or sha(path) != spec["sha256"]:
            fail("Backup integrity failure: " + name)
    return manifest


def prepare(args):
    manifest = check_backup(args.backup)
    if manifest["device"]["model"] != "komodo":
        fail("Repair preparation currently supports Pixel 9 Pro XL (komodo) only")
    original = json.loads(Path(args.original_imeis).read_text())
    if original.get("serial") != manifest["device"]["serial"]:
        fail("Original IMEI record belongs to another device")
    if original.get("confirmed_original") is not True:
        fail("Original IMEI record must attest confirmed_original=true; do not invent identifiers")
    imeis = {key: original.get(key) for key in ("imei1", "imei2")}
    data = (Path(args.backup) / "devinfo.img").read_bytes()
    patched, offsets = patch_devinfo(data, imeis)
    if patched == data:
        fail("devinfo already has these IMEIs; investigate modem/EFS instead")
    out = private_dir(args.out)
    (out / "devinfo-original.img").write_bytes(data)
    (out / "devinfo-restored.img").write_bytes(patched)
    plan = {
        "schema": 1, "device": manifest["device"],
        "backup": str(Path(args.backup).resolve()),
        "original_sha256": hashlib.sha256(data).hexdigest(),
        "restored_sha256": hashlib.sha256(patched).hexdigest(),
        "offsets": offsets, "changed_bytes": sum(a != b for a, b in zip(data, patched)),
        "scope": "devinfo-only; no EFS, cpsha, bootmode or bootloader changes",
        "compatibility": "Experimental. No Android 17 hardware validation.",
    }
    write_json(out / "plan.json", plan)
    print(f"Prepared {plan['changed_bytes']} changed bytes in {out}. Phone unchanged.")
    print(plan["compatibility"])


def check_plan(folder):
    folder = Path(folder).resolve()
    plan = json.loads((folder / "plan.json").read_text())
    if plan.get("schema") != 1 or plan["device"]["model"] != "komodo":
        fail("Unsupported repair plan")
    manifest = check_backup(plan["backup"])
    if manifest["device"] != plan["device"]:
        fail("Plan and backup device metadata differ")
    before = (folder / "devinfo-original.img").read_bytes()
    after = (folder / "devinfo-restored.img").read_bytes()
    if hashlib.sha256(before).hexdigest() != plan["original_sha256"] or hashlib.sha256(after).hexdigest() != plan["restored_sha256"]:
        fail("Repair plan image hash mismatch")
    if plan["original_sha256"] != manifest["files"]["devinfo.img"]["sha256"]:
        fail("Original image does not match verified backup")
    offsets = imei_offsets(before)
    if offsets != plan["offsets"] or len(before) != len(after):
        fail("Repair image layout or length changed")
    imeis = {key: after[offset:offset+15].decode("ascii") for key, offset in offsets.items()}
    expected, _ = patch_devinfo(before, imeis)
    if expected != after:
        fail("Repair image changes bytes outside the two IMEI fields")
    return plan


def apply(args):
    plan = check_plan(args.plan)
    if not args.apply:
        print(json.dumps(plan, indent=2))
        print("Dry run. To write, pass --apply --confirm-original-imeis --allow-unverified-build.")
        return
    if not args.confirm_original_imeis or not args.allow_unverified_build:
        fail("Writing requires --confirm-original-imeis and --allow-unverified-build")
    if args.serial != plan["device"]["serial"]:
        fail("Selected serial differs from repair plan")
    device = Device(args.serial, args.adb)
    if device.info() != plan["device"]:
        fail("Device properties changed since backup; make a fresh backup and plan")
    device.require_root()
    if device.digest("devinfo") != plan["original_sha256"]:
        fail("Live devinfo changed since backup; refusing a stale plan")
    image = Path(args.plan).resolve() / "devinfo-restored.img"
    if device.size("devinfo") != image.stat().st_size:
        fail("Live devinfo size differs from prepared image")
    # Use a unique remote staging directory, not the upstream's shared /tmp.
    remote = device.call("shell", "mktemp", "-d", "/data/local/tmp/pixel-restore.XXXXXXXX").decode().strip()
    if not re.fullmatch(r"/data/local/tmp/pixel-restore\.[A-Za-z0-9]+", remote):
        fail("Unexpected staging path")
    try:
        device.call("push", str(image), remote + "/devinfo.img", timeout=180)
        staged_hash = device.root("sha256sum " + remote + "/devinfo.img").decode().split()[0]
        if staged_hash != plan["restored_sha256"]:
            fail("Device staging hash mismatch")
        print("Writing devinfo only; do not disconnect power or USB.", flush=True)
        device.root("dd if=" + remote + "/devinfo.img of=/dev/block/by-name/devinfo bs=4096 conv=fsync", timeout=180)
        if device.digest("devinfo") != plan["restored_sha256"]:
            fail("WRITE VERIFICATION FAILED. Keep backups; do not reboot before investigation")
        print("devinfo write verified. Reboot manually, then retry sign-in and run diagnose.")
    finally:
        # Do not mask a partial-write error with a cleanup error.
        try:
            device.call("shell", "rm", "-f", remote + "/devinfo.img")
            device.call("shell", "rmdir", remote)
        except (ValueError, OSError):
            print("Staging cleanup failed: " + remote, file=sys.stderr)


def parser():
    p = argparse.ArgumentParser(description=__doc__, prog="pixel-restore modem")
    sub = p.add_subparsers(dest="command", required=True)
    sources = sub.add_parser("sources", help="Show pinned upstream tools and review findings")
    sources.set_defaults(func=lambda a: print(json.dumps(SOURCES, indent=2)))
    fetch = sub.add_parser("fetch-source", help="Download pinned source for review; never execute it")
    fetch.add_argument("name", choices=SOURCES)
    fetch.add_argument("--out", required=True)
    fetch.set_defaults(func=source_fetch)
    diag = sub.add_parser("diagnose", help="Read system properties and classify sign-in failures")
    diag.add_argument("--out", help="Write a new JSON report (raw logs are not saved)")
    diag.set_defaults(func=diagnose)
    back = sub.add_parser("backup", help="Read and verify modem partitions; requires existing root")
    back.add_argument("--out", required=True, help="New private backup directory")
    back.set_defaults(func=backup)
    prep = sub.add_parser("prepare", help="Prepare a devinfo-only repair offline; phone unchanged")
    prep.add_argument("--backup", required=True)
    prep.add_argument("--original-imeis", required=True, help="JSON with serial, imei1, imei2, confirmed_original")
    prep.add_argument("--out", required=True)
    prep.set_defaults(func=prepare)
    verify = sub.add_parser("verify-backup", help="Verify all backup sizes and SHA-256 hashes offline")
    verify.add_argument("backup")
    verify.set_defaults(func=lambda a: (check_backup(a.backup), print("Backup integrity verified")))
    restore = sub.add_parser("restore-devinfo", help="Preview repair, or explicitly apply to selected device")
    restore.add_argument("--plan", required=True)
    restore.add_argument("--apply", action="store_true")
    restore.add_argument("--confirm-original-imeis", action="store_true")
    restore.add_argument("--allow-unverified-build", action="store_true")
    restore.set_defaults(func=apply)
    for child in (diag, back, restore):
        child.add_argument("--serial", required=True, help="Exact adb serial; never auto-select")
        child.add_argument("--adb", default="adb", help="ADB executable")
    return p


def main():
    os.umask(0o077)
    try:
        args = parser().parse_args()
        args.func(args)
        return 0
    except (ValueError, OSError, KeyError, TypeError, UnicodeError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
