#!/usr/bin/env python3
"""Repair a blanked or damaged modem `efs` on a Tensor Pixel (Python 3.9+).

The modem keeps its calibration and NV data in four small partitions that no
factory image carries: `efs`, its mirror `efs_backup`, `modem_userdata` and
`persist`. When one of them is blank (every byte zero or 0xFF) or no longer a
filesystem, the only valid source for its contents is the same phone: the
surviving mirror, or a backup taken from it earlier. This tool reads the
partitions, says which are healthy, prepares a reviewed plan of what to write
where, and only writes with `apply --apply`.

    pixel_efs.py fetch   --out DIR                 # read the five partitions (fastbootd or rooted adb)
    pixel_efs.py inspect DIR                       # classify each image, compare efs with efs_backup
    pixel_efs.py plan    --images DIR --out PLAN   # decide the source for each unhealthy partition
    pixel_efs.py apply   --plan PLAN               # dry run; add --apply to flash from the bootloader

Nothing here knows or writes an IMEI. A phone whose efs and efs_backup are
both gone, with no backup, cannot get its calibration back from this tool; it
can only be given empty filesystems so Android boots again (`--allow-empty`).
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
from datetime import datetime, timezone

MODEM_PARTITIONS = ("efs", "efs_backup", "modem_userdata", "persist")
# Read alongside the modem partitions so a plan can be tied to the phone and
# so the IMEI records stay available for pixel_modem's checks.
READ_PARTITIONS = MODEM_PARTITIONS + ("devinfo",)
# efs and efs_backup are kept as copies of each other by the modem
# (`AT+GOOGBACKUPNV` rewrites the backup from efs, per bitdomo/restore_imei).
MIRRORS = {"efs": "efs_backup", "efs_backup": "efs"}
# Partitions Android formats itself when it finds them wiped: their fstab lines
# carry `formattable` (device/google/gs101 conf/fstab.gs101, device/google/zuma
# conf/fstab.efs). persist's line is not public, so it is never emptied here.
FORMATTABLE = ("efs", "efs_backup", "modem_userdata")
# Modem NV files seen in /mnt/vendor/efs by the reviewed upstream tools
# (davwheat's clear_nvitems.sh, bitdomo's efs_imei_write.sh). Not a full list.
NV_FILES = ("nv_normal.bin", "nv_normal.bin.md5", "nv_protected.bin")
MAX_PARTITION = 256 * 1024 * 1024
# fs_mgr_mount_all formats a `formattable` partition whose mount failed when
# it judges the partition wiped; a zero-filled image is wiped by any reading.
WIPED_PROBE = 4096

EXT4_MAGIC = 0xEF53
F2FS_MAGIC = 0xF2F52010


def fail(message):
    raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def private_dir(path):
    path = Path(path)
    if path.exists():
        fail(f"{path} already exists; choose a new directory")
    path.mkdir(parents=True, mode=0o700)
    return path


def sibling(name):
    """A module from this file's directory, or None when it is not there."""
    path = Path(__file__).with_name(name + ".py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------- image parsing

def ext4_info(data):
    """Superblock facts if `data` is an ext2/3/4 image, else None."""
    sb = data[1024:2048]
    if len(sb) < 1024 or int.from_bytes(sb[0x38:0x3A], "little") != EXT4_MAGIC:
        return None
    block = 1024 << int.from_bytes(sb[0x18:0x1C], "little")
    blocks = int.from_bytes(sb[0x4:0x8], "little")
    if int.from_bytes(sb[0x60:0x64], "little") & 0x80:  # INCOMPAT_64BIT
        blocks |= int.from_bytes(sb[0x150:0x154], "little") << 32
    state = int.from_bytes(sb[0x3A:0x3C], "little")
    return {
        "label": sb[0x78:0x88].rstrip(b"\0").decode("ascii", "replace"),
        "uuid": sb[0x68:0x78].hex(),
        "block_size": block,
        "blocks": blocks,
        "fs_bytes": blocks * block,
        "clean": state & 1 == 1,
        "errors": state & 2 == 2,
        "last_write": int.from_bytes(sb[0x30:0x34], "little"),
        "mount_count": int.from_bytes(sb[0x34:0x36], "little"),
    }


def f2fs_info(data):
    """Superblock facts if `data` is an f2fs image, else None. f2fs keeps two
    copies of the superblock (offset 1024 in blocks 0 and 1); both are read."""
    copies = []
    for base in (1024, 4096 + 1024):
        sb = data[base:base + 3072]
        if len(sb) < 0x7C + 1024 or int.from_bytes(sb[0:4], "little") != F2FS_MAGIC:
            copies.append(None)
            continue
        copies.append({
            "version": f"{int.from_bytes(sb[4:6], 'little')}.{int.from_bytes(sb[6:8], 'little')}",
            "block_size": 1 << int.from_bytes(sb[0x10:0x14], "little"),
            "blocks": int.from_bytes(sb[0x24:0x2C], "little"),
            "uuid": sb[0x6C:0x7C].hex(),
            "label": sb[0x7C:0x7C + 1024].decode("utf-16-le", "replace").split("\0", 1)[0],
        })
    primary = copies[0] or copies[1]
    if primary is None:
        return None
    info = dict(primary)
    info["fs_bytes"] = info["blocks"] * info["block_size"]
    info["superblocks"] = sum(1 for c in copies if c)
    info["superblocks_agree"] = copies[0] == copies[1]
    return info


def ext4_files(data):
    """[(path, size)] of the regular files in an ext4 image, via the
    read-only walker in android_fs.py; a string says why it could not."""
    afs = sibling("android_fs")
    if afs is None:
        return "android_fs.py not found next to this tool"
    try:
        fs = afs.Ext4(afs.Bytes(data))
        return [(path, size) for path, size, _ in fs.walk()]
    except (afs.Error, ValueError, IndexError, KeyError) as exc:
        return f"directory walk failed: {exc}"


def classify(data):
    """What an image is and whether it can serve as a source."""
    size = len(data)
    entry = {"size": size, "sha256": sha(data), "wiped_for_fs_mgr": size > 0 and not any(data[:WIPED_PROBE])}
    if size == 0:
        entry.update(kind="empty", health="blank", reason="zero bytes read")
        return entry
    if data.count(data[:1]) == size:
        kind = "zero" if data[0] == 0 else ("erased" if data[0] == 0xFF else "constant")
        entry.update(kind=kind, health="blank", reason=f"every byte is {data[0]:#04x}")
        return entry
    ext4, f2fs = ext4_info(data), f2fs_info(data)
    if ext4:
        entry["kind"], entry["ext4"] = "ext4", ext4
        files = ext4_files(data)
        if isinstance(files, str):
            entry["files_error"] = files
        else:
            entry["files"] = [{"path": p, "size": s} for p, s in sorted(files)]
            entry["nv_files"] = sorted(p.rsplit("/", 1)[-1] for p, _ in files if p.rsplit("/", 1)[-1] in NV_FILES)
        fits, clean = ext4["fs_bytes"] <= size, ext4["clean"] and not ext4["errors"]
        if not fits:
            entry.update(health="damaged", reason=f"filesystem claims {ext4['fs_bytes']} bytes in a {size}-byte image")
        elif "files_error" in entry:
            entry.update(health="damaged", reason=entry["files_error"])
        elif not entry["files"]:
            entry.update(health="empty", reason="ext4 with no files (freshly formatted?)")
        else:
            entry.update(health="healthy", reason="ext4" + ("" if clean else ", not cleanly unmounted"))
        return entry
    if f2fs:
        entry["kind"], entry["f2fs"] = "f2fs", f2fs
        if f2fs["fs_bytes"] > size:
            entry.update(health="damaged", reason=f"filesystem claims {f2fs['fs_bytes']} bytes in a {size}-byte image")
        elif not f2fs["superblocks_agree"]:
            entry.update(health="damaged", reason="the two f2fs superblocks differ")
        else:
            entry.update(health="healthy", reason="f2fs (contents not listed: no f2fs reader here)")
        return entry
    distinct = len(set(data[:1 << 20]))
    entry.update(kind="unknown", health="damaged",
                 reason=f"no ext4 or f2fs superblock; {distinct} distinct byte values in the first MiB")
    return entry


def load_images(folder, names=READ_PARTITIONS):
    folder = Path(folder)
    images = {}
    for name in names:
        path = folder / (name + ".img")
        if path.is_file():
            if path.stat().st_size > MAX_PARTITION:
                fail(f"{path} is larger than any modem partition ({MAX_PARTITION} bytes)")
            images[name] = path.read_bytes()
    if not images:
        fail(f"no efs.img / efs_backup.img / modem_userdata.img / persist.img in {folder}")
    return images


def inspect_images(images):
    """Per-image classification plus the cross-checks between them."""
    report = {"partitions": {name: classify(data) for name, data in images.items()}, "checks": {}}
    parts = report["partitions"]
    if "efs" in images and "efs_backup" in images:
        report["checks"]["efs_backup_same_size"] = len(images["efs"]) == len(images["efs_backup"])
        report["checks"]["efs_matches_efs_backup"] = images["efs"] == images["efs_backup"]
        a, b = parts["efs"], parts["efs_backup"]
        if a.get("files") is not None and b.get("files") is not None:
            fa = {f["path"]: f["size"] for f in a["files"]}
            fb = {f["path"]: f["size"] for f in b["files"]}
            report["checks"]["only_in_efs"] = sorted(set(fa) - set(fb))
            report["checks"]["only_in_efs_backup"] = sorted(set(fb) - set(fa))
            report["checks"]["same_name_different_size"] = sorted(p for p in fa.keys() & fb.keys() if fa[p] != fb[p])
        if a.get("ext4") and b.get("ext4"):
            newer = "efs" if a["ext4"]["last_write"] >= b["ext4"]["last_write"] else "efs_backup"
            report["checks"]["last_written"] = newer
    for name in ("efs", "efs_backup"):
        if name in parts and parts[name].get("files") is not None:
            missing = sorted(set(NV_FILES) - set(parts[name].get("nv_files", [])))
            report["checks"][f"{name}_nv_files_missing"] = missing
    report["healthy"] = sorted(n for n, p in parts.items() if p["health"] == "healthy")
    report["unhealthy"] = sorted(n for n, p in parts.items() if n in MODEM_PARTITIONS and p["health"] != "healthy")
    return report


def inspect(args):
    folder = Path(args.folder)
    images = load_images(folder)
    report = inspect_images(images)
    meta = folder / "report.json"
    if meta.is_file():
        try:
            report["device"] = json.loads(meta.read_text()).get("device")
        except (ValueError, OSError):
            pass
    if args.json or not sys.stdout.isatty():
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        render(report)
    if args.out:
        write_json(args.out, report)


def render(report):
    dev = report.get("device") or {}
    if dev:
        print(f"phone {dev.get('serial', '?')} ({dev.get('product', '?')}), read via {dev.get('mode', '?')}")
    for name, p in report["partitions"].items():
        flag = {"healthy": "ok     ", "blank": "BLANK  ", "damaged": "DAMAGED", "empty": "EMPTY  "}[p["health"]]
        print(f"{flag} {name:<15} {p['size']:>10} bytes  {p['kind']:<8} {p['reason']}")
        fs = p.get("ext4") or p.get("f2fs")
        if fs:
            print(f"        label {fs['label']!r}, uuid {fs['uuid']}, {fs['fs_bytes']} bytes of filesystem")
        if p.get("files") is not None:
            print(f"        {len(p['files'])} files; modem NV files present: {', '.join(p.get('nv_files') or []) or 'none'}")
    for key, value in report["checks"].items():
        print(f"check  {key}: {value}")


# ------------------------------------------------------------------ devices

def run(argv, timeout=120, stdout=None):
    try:
        result = subprocess.run(argv, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{argv[0]} timed out") from exc
    except OSError as exc:
        raise ValueError(f"cannot run {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        text = (result.stderr or b"").decode(errors="replace").strip()
        raise ValueError(f"{' '.join(argv[:3])}... failed: {text or result.returncode}")
    return result.stdout or b""


def attached(tool, states):
    out = run([tool, "devices"]).decode(errors="replace")
    return [cols[0] for cols in (l.split() for l in out.splitlines())
            if len(cols) >= 2 and cols[1] in states]


def choose_serial(wanted, serials, what):
    if wanted:
        if wanted not in serials:
            fail(f"{wanted} is not attached in {what}")
        return wanted
    if len(serials) == 1:
        return serials[0]
    if not serials:
        fail(f"no {what} device attached")
    fail(f"{len(serials)} {what} devices attached ({', '.join(serials)}); pick one with --serial")


class Fastboot:
    def __init__(self, serial=None, fastboot="fastboot"):
        self.fastboot = fastboot
        self.serial = choose_serial(serial, attached(fastboot, ("fastboot", "fastbootd")), "fastboot")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", self.serial):
            fail("invalid serial")

    def getvar(self, name):
        result = subprocess.run([self.fastboot, "-s", self.serial, "getvar", name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        for line in (result.stderr + result.stdout).decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("(bootloader)"):
                line = line[len("(bootloader)"):].strip()
            if line.startswith(name + ":"):
                return line[len(name) + 1:].strip() or None
        return None

    def partition_size(self, name):
        value = self.getvar("partition-size:" + name)
        if value is None:
            return None
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None

    def identity(self):
        return {"serial": self.serial, "product": self.getvar("product"), "unlocked": self.getvar("unlocked"),
                "userspace": self.getvar("is-userspace"), "bootloader": self.getvar("version-bootloader"),
                "baseband": self.getvar("version-baseband")}

    def fetch(self, partition, path):
        """None when the partition was read into `path`, else fastboot's reason."""
        try:
            result = subprocess.run([self.fastboot, "-s", self.serial, "fetch", partition, str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600, check=False)
        except subprocess.TimeoutExpired:
            return "timed out"
        if result.returncode == 0 and Path(path).is_file() and Path(path).stat().st_size:
            return None
        Path(path).unlink(missing_ok=True)
        lines = [l.strip() for l in (result.stderr + result.stdout).decode(errors="replace").splitlines() if l.strip()]
        return next((l for l in lines if l.startswith("FAILED")), lines[-1] if lines else f"exit {result.returncode}")

    def flash(self, partition, path):
        run([self.fastboot, "-s", self.serial, "flash", partition, str(path)], timeout=600)


class Adb:
    def __init__(self, serial=None, adb="adb"):
        self.adb = adb
        self.serial = choose_serial(serial, attached(adb, ("device",)), "adb")
        if self.root("id -u").strip() != b"0":
            fail("a root shell is required to read partitions over adb; this tool does not root the phone")

    def root(self, command, timeout=120, stdout=None):
        return run([self.adb, "-s", self.serial, "exec-out", "su", "-c", command], timeout=timeout, stdout=stdout)

    def prop(self, name):
        return run([self.adb, "-s", self.serial, "shell", "getprop", name]).decode().strip()

    def identity(self):
        return {"serial": self.serial, "product": self.prop("ro.product.device"),
                "unlocked": "yes" if self.prop("ro.boot.flash.locked") == "0" else "no",
                "bootloader": self.prop("ro.bootloader"), "baseband": self.prop("gsm.version.baseband")}

    def partition_size(self, name):
        value = self.root("blockdev --getsize64 /dev/block/by-name/" + name).strip()
        return int(value) if value.isdigit() else None

    def fetch(self, partition, path):
        try:
            with Path(path).open("xb") as handle:
                self.root(f"dd if=/dev/block/by-name/{partition} bs=1048576", timeout=600, stdout=handle)
        except ValueError as exc:
            Path(path).unlink(missing_ok=True)
            return str(exc)
        return None


def fetch(args):
    """Read the modem partitions and devinfo into a new private folder, with
    report.json tying the images to the phone. Read-only."""
    if args.adb:
        phone, mode = Adb(args.serial, args.adb_bin), "adb"
    else:
        phone, mode = Fastboot(args.serial, args.fastboot_bin), "fastboot"
    ident = phone.identity()
    ident["mode"] = mode
    out = private_dir(args.out)
    report = {"device": ident, "read": {}, "failed": {}, "sizes": {}}
    for name in READ_PARTITIONS:
        size = phone.partition_size(name)
        report["sizes"][name] = size
        if size is not None and size > MAX_PARTITION:
            report["failed"][name] = f"{size} bytes is larger than this tool reads"
            continue
        path = out / (name + ".img")
        error = phone.fetch(name, path)
        if error:
            report["failed"][name] = error
        else:
            data = path.read_bytes()
            if size is not None and len(data) != size:
                report["failed"][name] = f"read {len(data)} bytes, partition is {size}"
                path.unlink()
            else:
                report["read"][name] = {"size": len(data), "sha256": sha(data)}
        print(f"{name}: {error or report['failed'].get(name) or 'read'}", file=sys.stderr)
    if mode == "fastboot" and not report["read"]:
        report["hint"] = ("the bootloader refuses fetch; boot the read-enabled fastbootd (fastbootd-read/) "
                          "or use --adb with a rooted Android" if ident.get("userspace") != "yes" else
                          "fastbootd refused every read; enable reading in its menu (fastbootd-read/)")
    write_json(out / "report.json", report)
    print(f"images in {out}; they hold this phone's modem identity and calibration, keep them private")
    if report["failed"]:
        print("not read: " + ", ".join(f"{k} ({v})" for k, v in report["failed"].items()), file=sys.stderr)
    if report["read"]:
        images = load_images(out)
        render(inspect_images(images))


# --------------------------------------------------------------------- plan

def read_meta(folder):
    path = Path(folder) / "report.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {}


def backup_images(folder):
    """Images from a `pixel_modem.py backup` folder (manifest.json with the
    device serial) or any folder of <partition>.img files, plus its serial."""
    folder = Path(folder)
    manifest = folder / "manifest.json"
    serial = None
    if manifest.is_file():
        spec = json.loads(manifest.read_text())
        serial = (spec.get("device") or {}).get("serial")
        for name, entry in (spec.get("files") or {}).items():
            path = folder / name
            if not path.is_file() or path.stat().st_size != entry.get("size") or sha(path.read_bytes()) != entry.get("sha256"):
                fail(f"backup integrity failure: {name}")
    else:
        serial = read_meta(folder).get("device", {}).get("serial")
    return load_images(folder, MODEM_PARTITIONS), serial


def decide(current, sizes, backup=None, prefer=None, allow_empty=False):
    """Per partition: {"action": "keep"|"flash"|"unresolved", "source": ..., "why": ...}.
    `current` is the inspect report of the phone's images, `sizes` the
    partition sizes known from the bootloader (None where unknown), `backup`
    an optional {name: classified image} from the same phone."""
    parts = current["partitions"]
    decisions = {}
    for name in MODEM_PARTITIONS:
        if name not in parts:
            decisions[name] = {"action": "unresolved", "why": "not read from the phone"}
            continue
        here = parts[name]
        size = sizes.get(name) or here["size"]
        if here["health"] == "healthy":
            mirror = MIRRORS.get(name)
            if mirror and prefer == mirror and parts.get(mirror, {}).get("health") == "healthy" \
                    and parts[mirror]["sha256"] != here["sha256"] and parts[mirror]["size"] == size:
                decisions[name] = {"action": "flash", "source": f"phone:{mirror}", "sha256": parts[mirror]["sha256"],
                                   "why": f"--prefer {mirror}: healthy but differs from {mirror}"}
            else:
                decisions[name] = {"action": "keep", "why": here["reason"]}
            continue
        why = [f"{name} is {here['health']}: {here['reason']}"]
        mirror = MIRRORS.get(name)
        if mirror and parts.get(mirror, {}).get("health") == "healthy":
            if parts[mirror]["size"] == size:
                decisions[name] = {"action": "flash", "source": f"phone:{mirror}", "sha256": parts[mirror]["sha256"],
                                   "why": f"{why[0]}; {mirror} is healthy and the same size"}
                continue
            why.append(f"{mirror} is healthy but {parts[mirror]['size']} bytes, not {size}")
        elif mirror:
            why.append(f"{mirror} is {parts.get(mirror, {}).get('health', 'not read')}")
        if backup and backup.get(name, {}).get("health") == "healthy":
            if backup[name]["size"] == size:
                decisions[name] = {"action": "flash", "source": f"backup:{name}", "sha256": backup[name]["sha256"],
                                   "why": "; ".join(why) + "; the backup copy is healthy and the same size"}
                continue
            why.append(f"backup {name} is {backup[name]['size']} bytes, not {size}")
        elif backup is not None:
            why.append(f"backup {name} is {backup.get(name, {}).get('health', 'absent')}")
        if allow_empty and name in FORMATTABLE:
            decisions[name] = {"action": "flash", "source": "empty", "sha256": sha(bytes(size)), "size": size,
                               "why": "; ".join(why) + "; --allow-empty: a zero image Android formats at boot "
                               "(calibration is NOT recovered)"}
            continue
        if allow_empty:
            why.append("persist is not known to be formattable, so it is never emptied")
        if here["health"] == "empty":
            decisions[name] = {"action": "keep", "why": "; ".join(why) + "; an empty filesystem mounts, "
                               "so it stays (nothing to refill it from)"}
            continue
        decisions[name] = {"action": "unresolved", "why": "; ".join(why)}
    return decisions


def plan(args):
    images = load_images(args.images)
    meta = read_meta(args.images)
    device = meta.get("device") or {}
    serial = args.serial or device.get("serial")
    if not serial:
        fail("the images folder has no report.json naming the phone; pass --serial")
    if device.get("serial") and args.serial and args.serial != device["serial"]:
        fail(f"--serial {args.serial} but the images were read from {device['serial']}")
    current = inspect_images(images)
    sizes = {k: v for k, v in (meta.get("sizes") or {}).items() if v}
    backup = None
    if args.backup:
        backup_data, backup_serial = backup_images(args.backup)
        if backup_serial and backup_serial != serial:
            fail(f"backup is from {backup_serial}, not {serial}; another phone's efs would give this one "
                 "the wrong identity and calibration")
        if not backup_serial and not args.backup_is_this_phone:
            fail("the backup names no serial; pass --backup-is-this-phone after checking it was taken "
                 "from this phone")
        backup = {name: classify(data) for name, data in backup_data.items()}
    decisions = decide(current, sizes, backup, args.prefer, args.allow_empty)
    out = private_dir(args.out)
    actions = []
    for name, d in decisions.items():
        if d["action"] != "flash":
            continue
        kind, _, src = d["source"].partition(":")
        if kind == "phone":
            data = images[src]
        elif kind == "backup":
            data = backup_data[src]
        else:
            data = bytes(d["size"])
        assert sha(data) == d["sha256"]
        target = out / f"{name}.img"
        target.write_bytes(data)
        actions.append({"partition": name, "image": target.name, "size": len(data), "sha256": d["sha256"],
                        "source": d["source"]})
    result = {
        "schema": 1, "created": datetime.now(timezone.utc).isoformat(),
        "device": {"serial": serial, "product": device.get("product")},
        "images": str(Path(args.images).resolve()), "backup": str(Path(args.backup).resolve()) if args.backup else None,
        "partition_sizes": sizes, "current": {n: {k: p[k] for k in ("health", "kind", "reason", "size", "sha256")}
                                             for n, p in current["partitions"].items()},
        "decisions": decisions, "actions": actions,
        "unresolved": [n for n, d in decisions.items() if d["action"] == "unresolved"],
    }
    write_json(out / "plan.json", result)
    for name, d in decisions.items():
        print(f"{d['action']:<10} {name:<15} {d.get('source', '')!s:<20} {d['why']}")
    if actions:
        print(f"\nplan written to {out}: {len(actions)} partition(s) to flash on {serial}. Review it, then\n"
              f"  python3 {Path(__file__).name} apply --plan {out}            # dry run\n"
              f"  python3 {Path(__file__).name} apply --plan {out} --apply    # flash from the bootloader")
    else:
        print(f"\nnothing to flash; plan written to {out}")
    if result["unresolved"]:
        print("unresolved: " + ", ".join(result["unresolved"]) + " (no healthy source on this phone or in the backup)")


# -------------------------------------------------------------------- apply

def check_plan(folder):
    folder = Path(folder).resolve()
    spec = json.loads((folder / "plan.json").read_text())
    if spec.get("schema") != 1:
        fail("unsupported plan")
    for action in spec["actions"]:
        if action["partition"] not in MODEM_PARTITIONS:
            fail(f"plan names a partition this tool does not write: {action['partition']}")
        path = folder / action["image"]
        if path.name != f"{action['partition']}.img" or not path.is_file() or path.is_symlink():
            fail(f"plan image missing or misnamed: {action['image']}")
        data = path.read_bytes()
        if len(data) != action["size"] or sha(data) != action["sha256"]:
            fail(f"plan image changed since it was written: {action['image']}")
    return spec


def apply(args):
    folder = Path(args.plan).resolve()
    spec = check_plan(folder)
    serial = spec["device"]["serial"]
    if not spec["actions"]:
        print("the plan has nothing to flash")
        return
    for a in spec["actions"]:
        print(f"flash {a['partition']:<15} <- {a['image']} ({a['size']} bytes, {a['source']}, sha256 {a['sha256'][:16]}...)")
    if spec.get("unresolved"):
        print("left as they are (no source): " + ", ".join(spec["unresolved"]))
    if not args.apply:
        print(f"dry run for {serial}; add --apply to flash")
        return
    phone = Fastboot(args.serial or serial, args.fastboot_bin)
    ident = phone.identity()
    if phone.serial != serial:
        fail(f"phone in fastboot is {phone.serial}; the plan is for {serial}")
    if spec["device"].get("product") and ident.get("product") != spec["device"]["product"]:
        fail(f"phone is {ident.get('product')}, plan was made on {spec['device']['product']}")
    if ident.get("unlocked") != "yes":
        fail("the bootloader is locked; it refuses to flash efs. This tool does not unlock (that wipes data)")
    if ident.get("userspace") == "yes" and not args.allow_fastbootd:
        fail("the phone is in fastbootd; reboot to the bootloader (`fastboot reboot bootloader`) to flash, "
             "or pass --allow-fastbootd")
    for a in spec["actions"]:
        live = phone.partition_size(a["partition"])
        if live is not None and live != a["size"]:
            fail(f"{a['partition']} is {live} bytes on the phone, the image is {a['size']}")
    print(f"phone {serial} ({ident.get('product')}), bootloader {ident.get('bootloader')}, baseband {ident.get('baseband')}")
    if not args.yes:
        typed = input(f"type the serial {serial} to flash the {len(spec['actions'])} partition(s) above: ").strip()
        if typed != serial:
            fail("serial not confirmed; nothing flashed")
    done = []
    for a in spec["actions"]:
        print(f"fastboot flash {a['partition']} {a['image']}  (do not unplug)", flush=True)
        phone.flash(a["partition"], folder / a["image"])
        done.append(a["partition"])
    print("flashed: " + ", ".join(done))
    print("next: `fastboot reboot`, let Android come up (it formats any empty image itself), then "
          "check the IMEI with *#06# and run `pixel_modem.py diagnose`. Fastboot cannot read the "
          "partitions back; a fresh `fetch` through fastbootd or rooted adb verifies them.")


# ---------------------------------------------------------------------- cli

def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Read efs, efs_backup, modem_userdata, persist and devinfo (read-only)")
    f.add_argument("--out", required=True, help="New private folder for the images and report.json")
    f.add_argument("--adb", action="store_true", help="Read with a rooted adb shell instead of fastboot fetch")
    f.set_defaults(func=fetch)

    i = sub.add_parser("inspect", help="Classify the images in a folder and compare efs with efs_backup")
    i.add_argument("folder")
    i.add_argument("--json", action="store_true")
    i.add_argument("--out", help="Also write the report as JSON")
    i.set_defaults(func=inspect)

    pl = sub.add_parser("plan", help="Decide what to write to each unhealthy partition; phone unchanged")
    pl.add_argument("--images", required=True, help="Folder from `fetch` (the phone's current state)")
    pl.add_argument("--out", required=True, help="New private folder for plan.json and the images to flash")
    pl.add_argument("--backup", help="Earlier backup of this same phone (`pixel_modem.py backup` folder)")
    pl.add_argument("--backup-is-this-phone", action="store_true",
                    help="Accept a backup folder that names no serial")
    pl.add_argument("--prefer", choices=("efs", "efs_backup"),
                    help="When both copies are healthy but differ, overwrite the other with this one")
    pl.add_argument("--allow-empty", action="store_true",
                    help="With no source at all, write a zero image so Android formats the partition "
                         "(the phone boots; calibration is not recovered)")
    pl.add_argument("--serial", help="Phone serial, when the images folder has no report.json")
    pl.set_defaults(func=plan)

    ap = sub.add_parser("apply", help="Show a plan, or flash it from the bootloader with --apply")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--apply", action="store_true", help="Actually flash (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="Skip typing the serial to confirm")
    ap.add_argument("--allow-fastbootd", action="store_true", help="Flash from fastbootd instead of the bootloader")
    ap.set_defaults(func=apply)

    for child in (f, ap):
        child.add_argument("--serial", help="Device serial; optional when one device is attached")
        child.add_argument("--fastboot-bin", default="fastboot", help="fastboot executable")
    f.add_argument("--adb-bin", default="adb", help="adb executable")
    return p


def main(argv=None):
    os.umask(0o077)
    try:
        args = parser().parse_args(argv)
        args.func(args)
        return 0
    except (ValueError, OSError, KeyError, TypeError, UnicodeError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
