#!/usr/bin/env python3
"""Pixel modem diagnostics and original-IMEI repair preparation (Python 3.9+).

Independent implementation; upstream sources are fetched at pinned revisions for
review, never imported or executed. Every device command passes an explicit
serial to adb/fastboot; when --serial is omitted the only attached device is
used, and more than one is an error.
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
import time
import zipfile
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


def attached(tool, state):
    """Serials `<tool> devices` lists in `state` ("device" for adb, "fastboot")."""
    out = run([tool, "devices"]).decode(errors="replace")
    states = {"fastboot": ("fastboot", "fastbootd")}.get(state, (state,))
    return [cols[0] for cols in (l.split() for l in out.splitlines())
            if len(cols) >= 2 and cols[1] in states]


def choose_serial(wanted, serials, what):
    """The serial asked for, or the only attached one; never a guess among several."""
    if wanted:
        return wanted
    if len(serials) == 1:
        return serials[0]
    if not serials:
        fail(f"No {what} device attached")
    fail(f"{len(serials)} {what} devices attached ({', '.join(serials)}); pick one with --serial")


def check_serial(serial):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
        fail("Invalid serial")
    return serial


class Device:
    def __init__(self, serial, adb="adb"):
        serial = check_serial(serial or choose_serial(None, attached(adb, "device"), "adb"))
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


def parse_getvar_all(text):
    """`fastboot getvar all` as {name: value}. Names can contain colons
    (`partition-size:efs: 0x...`), so the value starts after the last ": ",
    or the last ":" on bootloaders that print no space."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("(bootloader)"):
            continue
        line = line[len("(bootloader)"):].strip()
        name, sep, value = line.rpartition(": ")
        if not sep:
            name, sep, value = line.rpartition(":")
        if sep and name:
            found[name.strip()] = value.strip()
    return found


def parse_oem_config(text):
    """`(bootloader) name: value` or `(bootloader) name=value` lines from an
    oem query; status lines (OKAY, FAILED, Finished) are dropped."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("(bootloader)"):
            continue
        line = line[len("(bootloader)"):].strip()
        for sep in (": ", "=", ":"):
            name, found_sep, value = line.partition(sep)
            if found_sep and name.strip():
                found[name.strip()] = value.strip()
                break
    return found


# Partitions that hold modem firmware, calibration or identity on Tensor Pixels.
MODEM_PARTITION_RE = re.compile(r"^(modem|radio|efs|persist|devinfo|nv|cp|ril|vbmeta_modem)", re.I)


# Security components whose getvars are `<component>-<field>`.
COMPONENT_RE = re.compile(r"^(ap|gsa|gsc|tpu|aoc|ldfw|tzsw|bl1|bl2|bl31|pbl|abl)-(.+)$")
# Titan M generations by the first letter of the gsc-flavor getvar, after
# their Cr50-family board names: Citadel (Titan M, Pixel 3-5) and Dauntless
# (Titan M2, Pixel 6 on). Inferred from those names, not documented by Google.
TITAN_FLAVORS = {"c": "Titan M (Citadel)", "d": "Titan M2 (Dauntless)"}


def titan_model(flavor):
    return TITAN_FLAVORS.get(flavor[:1].lower(), f"unknown Titan M flavor {flavor}")


PARTITION_FIELDS = {"partition-size": "size", "partition-type": "type", "is-logical": "logical"}
SLOT_FIELDS = {"slot-successful": "successful", "slot-unbootable": "unbootable",
               "slot-retry-count": "retry_count"}


def _value(field, value):
    if field == "size":
        try:
            return int(value, 16)
        except ValueError:
            return value
    if field in ("logical", "successful", "unbootable"):
        return {"yes": True, "no": False}.get(value.lower(), value)
    return value


def structure_getvar(variables):
    """Split getvar-all output into partition objects, slot objects and the
    variables that belong to neither. A slotted partition is one object with
    per-slot entries: {"boot": {"slots": {"a": {...}, "b": {...}}}}."""
    raw, has_slot, slots, rest = {}, {}, {}, {}
    for name, value in variables.items():
        field, _, target = name.partition(":")
        if field in PARTITION_FIELDS and target:
            raw.setdefault(target, {})[PARTITION_FIELDS[field]] = _value(PARTITION_FIELDS[field], value)
        elif field == "has-slot" and target:
            has_slot[target] = value.lower() == "yes"
        elif field.startswith("slot-") and target in ("a", "b"):
            key = SLOT_FIELDS.get(field) or field[len("slot-"):].replace("-", "_")
            slots.setdefault(target, {})[key] = _value("logical" if key != "retry_count" else key, value)
        else:
            rest[name] = value
    partitions = {}
    for name, fields in raw.items():
        base, sep, slot = name.rpartition("_")
        slotted = sep and slot in ("a", "b") and (
            has_slot.get(base) or f"{base}_{'b' if slot == 'a' else 'a'}" in raw)
        if slotted:
            partitions.setdefault(base, {"slots": {}})["slots"][slot] = fields
        else:
            partitions[name] = fields
    for base, yes in has_slot.items():
        if yes and base in partitions and "slots" not in partitions[base]:
            partitions[base]["slotted"] = True
    # `prefix:item` variables: an item naming a partition is attached to that
    # partition (`sbdp:abl` becomes partitions.abl.sbdp); the rest are grouped
    # under their prefix instead of listed flat.
    if slots:
        # Implied by the slot objects themselves.
        rest.pop("slot-suffixes", None)
        rest.pop("slot-count", None)
    variables, policy = {}, {}
    for name, value in rest.items():
        prefix, sep, item = name.partition(":")
        component = COMPONENT_RE.match(name) if not sep else None
        if component:
            comp, field = component.groups()
            if field.startswith("ar-"):
                # Per-world levels: ap-ar-s (secure), ap-ar-ns (non-secure).
                field = "anti_rollback_" + field[3:]
                value = int(value) if value.isdigit() else value
            elif field.endswith("-ar") or field == "ar":
                # Anti-rollback level of one firmware image: attach it to that
                # partition (gsa-bl1-ar -> gsa_bl1, gsa-fw-ar -> gsa).
                image = field[:-3] if field != "ar" else ""
                target = comp if image in ("", "fw") else f"{comp}_{image}"
                if target in partitions:
                    partitions[target]["anti_rollback"] = int(value) if value.isdigit() else value
                    continue
                field = (image + "_" if image else "") + "anti_rollback"
                value = int(value) if value.isdigit() else value
            variables.setdefault(comp, {})[field.replace("-", "_")] = value
            continue
        if not sep or not item:
            variables[name] = value
        elif item in partitions:
            partitions[item][prefix] = _value("logical", value)
            policy.setdefault(prefix, {})
        else:
            variables.setdefault(prefix, {})
            if isinstance(variables[prefix], dict):
                variables[prefix][item] = _value("logical", value)
            else:
                variables[name] = value
    gsc = variables.get("gsc")
    if isinstance(gsc, dict) and gsc.get("flavor"):
        gsc["chip"] = titan_model(gsc["flavor"])
    return {"partitions": dict(sorted(partitions.items())),
            "slots": dict(sorted(slots.items())),
            "variables": dict(sorted(variables.items()))}


def regroup(partitions, slots):
    """Present partitions the way the phone uses them: vbmeta_<x> attached to
    the partition <x> it verifies, and every slotted partition moved under
    its slot. Returns (unslotted partitions, slots with their partitions)."""
    parts = {k: dict(v) for k, v in partitions.items()}
    for name in [n for n in parts if n.startswith("vbmeta_")]:
        target = name[len("vbmeta_"):]
        if target not in parts:
            continue
        meta, entry = parts.pop(name), parts[target]
        if "slots" in meta and "slots" in entry:
            entry["slots"] = {s: dict(f, vbmeta=meta["slots"].get(s, {})) for s, f in entry["slots"].items()}
        else:
            entry["vbmeta"] = meta
    grouped = {s: dict(info) for s, info in slots.items()}
    unslotted = {}
    for name, entry in parts.items():
        if "slots" not in entry:
            unslotted[name] = entry
            continue
        shared = {k: v for k, v in entry.items() if k != "slots"}
        for slot, fields in entry["slots"].items():
            grouped.setdefault(slot, {}).setdefault("partitions", {})[name] = dict(fields, **shared)
    for info in grouped.values():
        if "partitions" in info:
            info["partitions"] = dict(sorted(info["partitions"].items()))
    return unslotted, dict(sorted(grouped.items()))


def partition_sizes(entry):
    """Every size a partition object records (one per slot when slotted)."""
    if "slots" in entry:
        return [s.get("size") for s in entry["slots"].values()]
    return [entry.get("size")]


GSC_IMAGES = ("ro_a", "ro_b", "rw_a", "rw_b")


def parse_gsc_image(text):
    """One `oem gsc version` image line, `[*] <version>/<build> <state>`,
    where * marks the image the Titan M is running. Unmatched text is kept."""
    match = re.fullmatch(r"(\*)?\s*([^\s/]+)/(\S+)\s+(\S+)", text.strip()) if isinstance(text, str) else None
    if not match:
        return text
    info = {"running": bool(match[1]), "image_version": match[2], "build": match[3], "state": match[4]}
    # RW builds look like nugget_v0.0.ab<build id>-<commit>-r; RO builds are the commit.
    tag = re.fullmatch(r"(.*?)\.?ab(\d+)-([0-9a-f]{6,})-(\w+)", match[3])
    if tag:
        info.update(build_id=tag[2], commit=tag[3])
    elif re.fullmatch(r"[0-9a-f]{6,}", match[3]):
        info["commit"] = match[3]
    return info


# Titan M images start with the SignedHeader that Cr50 (Google's H1 chip)
# also uses: chromiumos/platform/ec chip/g/signed_header.h and
# board/cr50/board_id.c. Magic -1 is H1; the bootloader names fffffffd
# Dauntless (Titan M2); fffffffe as Citadel (Titan M) is inferred.
GSC_HEADER_MAGIC = {0xFFFFFFFF: "H1 (Cr50)", 0xFFFFFFFE: "Citadel (Titan M)",
                    0xFFFFFFFD: "Dauntless (Titan M2)"}
SIGNED_HEADER_PADDING = 0x33333333


def _hex(value):
    """The leading hex number of a header value such as `fffffffd (Dauntless)`."""
    match = re.match(r"\s*(?:0x)?([0-9a-fA-F]+)", str(value))
    return int(match[1], 16) if match else None


def _bits(text, set_bits=True):
    """Bit numbers (word by word, low bit first) that are set, or clear, in a
    map printed as consecutive 32-bit hex words."""
    text = re.sub(r"[^0-9a-fA-F]", "", str(text))
    found = []
    for w in range(len(text) // 8):
        word = int(text[w*8:w*8+8], 16)
        found += [w * 32 + b for b in range(32) if bool(word >> b & 1) == set_bits]
    return found


def parse_gsc_header(fields):
    """Decode `oem gsc header <image>` (hdr.<field>: <hex>) into named values;
    fields this does not know are kept as printed."""
    if not isinstance(fields, dict):
        return fields
    raw = {k[len("hdr."):] if k.startswith("hdr.") else k: v for k, v in fields.items()}
    out = {}
    for key, value in raw.items():
        number = _hex(value)
        if key == "magic" and number is not None:
            out[key] = {"value": f"{number:08x}", "chip": GSC_HEADER_MAGIC.get(number, "unknown")}
        elif key == "keyid" and number is not None:
            out[key] = {"value": f"{number:08x}", "signed_for": number}
        elif key == "tag":
            try:
                text = bytes.fromhex(str(value).strip()).rstrip(b"\0").decode("ascii")
            except (ValueError, UnicodeDecodeError):
                text = None
            out[key] = {"value": value, "text": text} if text and text.isprintable() else value
        elif key == "timestamp" and number is not None:
            out[key] = {"value": number, "utc": datetime.fromtimestamp(number, timezone.utc).isoformat()}
        elif key == "img_size" and number is not None:
            out[key] = human_size(number)
        elif key in ("ro_base", "ro_max", "rx_base", "rx_max") and number is not None:
            out[key] = f"{number:#010x}"
        elif key in ("epoch", "major", "minor") and number is not None:
            out[key] = number
        elif key == "fusemap":
            out[key] = {"value": value, "fuses_checked": _bits(value)}
        elif key == "infomap":
            # Cr50 clears infomap bits to raise its rollback level: the image
            # needs that many INFO words burned (inferred for Titan M).
            cleared = _bits(value, set_bits=False)
            out[key] = {"value": value, "cleared_bits": cleared, "rollback_level": len(cleared)}
        elif key == "board_id":
            words = [int(w, 16) for w in str(value).split() if re.fullmatch(r"[0-9a-fA-F]{1,8}", w)]
            if len(words) == 3:
                type_, mask, flags = words
                out[key] = {"type": f"{type_:08x}", "mask": f"{mask:08x}", "flags": f"{flags:08x}",
                            # Blank (all zero once decoded) runs on any board.
                            "any_board": not (type_ | mask | flags) or
                            all(w == SIGNED_HEADER_PADDING for w in words)}
            else:
                out[key] = value
        else:
            out[key] = value
    if isinstance(out.get("keyid"), dict):
        # Cr50's G_SIGNED_FOR_PROD: production keys have bit 2 set. Only
        # confirmed for H1; on Titan M chips the rule is unverified.
        rule = "production" if out["keyid"]["signed_for"] & 4 else "development"
        h1 = isinstance(out.get("magic"), dict) and out["magic"]["value"] == "ffffffff"
        out["keyid"]["signed_for"] = rule if h1 else f"{rule} (Cr50 rule, unconfirmed for this chip)"
    if all(k in out for k in ("epoch", "major", "minor")):
        out["image_version"] = f"{out['epoch']}.{out['major']}.{out['minor']}"
    return out


# Lock order of the Titan M AVB app (nugget/proto/nugget/app/avb/avb.proto,
# enum LockIndex): CARRIER, DEVICE, BOOT, OWNER.
GSC_AVB_LOCKS = ("carrier_lock", "device_lock", "boot_lock", "owner_lock")


def parse_gsc_avb_state(fields):
    """Name the fields of `oem gsc avb state`; unknown ones are kept. The
    meanings follow avb.proto's GetStateResponse (bootloader: the chip still
    takes bootloader-only requests; production: production mode is set)."""
    if not isinstance(fields, dict):
        return fields
    out, locks = {}, {}
    for key, value in fields.items():
        name = key.strip().rstrip(":").strip()
        value = str(value).strip().lstrip(":").strip()
        lock = re.fullmatch(r"Lock\[(\d+)\]", name, re.I)
        number = int(value) if re.fullmatch(r"\d+", value) else value
        if not value and name.lower().endswith("avb state"):
            continue  # the heading line
        if lock:
            index = int(lock[1])
            label = GSC_AVB_LOCKS[index] if index < len(GSC_AVB_LOCKS) else f"lock_{index}"
            locks[label] = {"locked": bool(number), "index": index} if isinstance(number, int) else value
        elif name.lower() == "version":
            out["avb_app_version"] = number
        elif name.lower() == "bootloader state":
            out["in_bootloader"] = bool(number) if isinstance(number, int) else value
        elif name.lower() == "production state":
            out["production"] = bool(number) if isinstance(number, int) else value
        elif name.lower() == "number of locks":
            out["lock_count"] = number
        else:
            out[name] = value
    if locks:
        out["locks"] = locks
    return out


def gsc_headers_match_versions(gsc):
    """Whether each image's header version (epoch.major.minor) is the version
    `oem gsc version` reports for it; None when there is nothing to compare."""
    if not isinstance(gsc, dict):
        return None
    headers, versions = gsc.get("firmware_headers"), gsc.get("firmware_version")
    if not isinstance(headers, dict) or not isinstance(versions, dict):
        return None
    lookup = {k.lower(): v for k, v in versions.items()}
    result = {}
    for image, header in headers.items():
        reported = lookup.get(image.lower())
        if isinstance(header, dict) and "image_version" in header and isinstance(reported, dict):
            result[image] = header["image_version"] == reported.get("image_version")
    return result or None


def gsc_running_matches(gsc):
    """Whether the running RO and RW images' commits are the ones getvar's
    gsc-ro and gsc-rw report; None when there is nothing to compare."""
    images = gsc.get("firmware_version") if isinstance(gsc, dict) else None
    if not isinstance(images, dict):
        return None
    result = {}
    for kind in ("ro", "rw"):
        running = [v for k, v in images.items()
                   if k.lower().startswith(kind + "_") and isinstance(v, dict) and v.get("running")]
        if running and gsc.get(kind):
            result[kind] = running[0].get("commit") == str(gsc[kind]).lower()
    return result or None


class Fastboot:
    """A phone in the bootloader. fastboot cannot read partitions back, so
    every write here is checked beforehand rather than verified afterwards."""

    def __init__(self, serial, fastboot="fastboot"):
        serial = serial or choose_serial(None, attached(fastboot, "fastboot"), "fastboot")
        self.serial, self.fastboot = check_serial(serial), fastboot
        if self.serial not in attached(fastboot, "fastboot"):
            fail(f"{self.serial} is not in fastboot")

    def call(self, *args, timeout=180):
        return run([self.fastboot, "-s", self.serial, *args], timeout=timeout)

    def query(self, name):
        # fastboot prints variables on stderr, which run() does not return.
        result = subprocess.run([self.fastboot, "-s", self.serial, "getvar", name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60, check=False)
        return (result.stderr + result.stdout).decode(errors="replace")

    def getvar(self, name):
        for line in self.query(name).splitlines():
            line = line.strip()
            if line.startswith("(bootloader)"):
                line = line[len("(bootloader)"):].strip()
            if line.startswith(name + ":"):
                value = line[len(name) + 1:].strip()
                return value or None
        return None

    def getvar_all(self):
        return parse_getvar_all(self.query("all"))

    def oem(self, *args):
        """Output of a read-only `fastboot oem` query, whatever its exit status."""
        result = subprocess.run([self.fastboot, "-s", self.serial, "oem", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60, check=False)
        return (result.stderr + result.stdout).decode(errors="replace")

    def oem_config(self):
        """`fastboot oem get_config all` as {name: value}, or {} when this
        bootloader has no such command (it is a read-only Pixel query)."""
        return parse_oem_config(self.oem("get_config", "all"))

    def gsc_version(self):
        """`fastboot oem gsc version`: {name: value} where the lines have that
        shape, else the bootloader's lines as they are; None if unsupported."""
        result = self.gsc_query("version")
        if isinstance(result, dict):
            result = {k: parse_gsc_image(v) for k, v in result.items()}
        return result

    def gsc_headers(self):
        """`fastboot oem gsc header <image>` for each Titan M firmware image
        (read-only and read-write, slots A and B), skipping unanswered ones."""
        headers = {image: parse_gsc_header(self.gsc_query("header", image)) for image in GSC_IMAGES}
        return {image: value for image, value in headers.items() if value}

    def gsc_avb_state(self):
        """`fastboot oem gsc avb state`: the Titan M's view of verified boot."""
        return parse_gsc_avb_state(self.gsc_query("avb", "state"))

    def gsc_query(self, *args):
        text = self.oem("gsc", *args)
        fields = parse_oem_config(text)
        if fields:
            return fields
        lines = [l.strip()[len("(bootloader)"):].strip() for l in text.splitlines()
                 if l.strip().startswith("(bootloader)") and l.strip() != "(bootloader)"]
        return lines or None

    def require(self, product):
        actual = self.getvar("product")
        if actual != product:
            fail(f"Phone in fastboot is {actual or 'unknown'}, expected {product}")
        if self.getvar("unlocked") != "yes":
            fail("Bootloader is locked; fastboot refuses to flash modem partitions. "
                 "This tool does not unlock it (unlocking wipes user data)")

    def fetch(self, partition, path, slot=None):
        """`fastboot fetch`: None when the image was read back, else the
        bootloader's reason. Most bootloaders refuse; fastbootd allows some.
        A slotted partition is named without its suffix and given --slot."""
        argv = [self.fastboot, "-s", self.serial] + (["--slot", slot] if slot else [])
        try:
            result = subprocess.run(argv + ["fetch", partition, str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=600, check=False)
        except subprocess.TimeoutExpired:
            return "timed out"
        if result.returncode == 0 and Path(path).is_file() and Path(path).stat().st_size:
            return None
        Path(path).unlink(missing_ok=True)
        text = (result.stderr + result.stdout).decode(errors="replace")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        reason = next((l for l in lines if l.startswith("FAILED")), lines[-1] if lines else "")
        return reason or f"fastboot exited {result.returncode}"

    def wait(self, seconds=60):
        deadline = time.monotonic() + seconds
        time.sleep(2)
        while time.monotonic() < deadline:
            if self.serial in attached(self.fastboot, "fastboot"):
                return
            time.sleep(0.5)
        fail(f"{self.serial} did not return to fastboot within {seconds} s")


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


FASTBOOT_VARS = {
    "product": "product", "bootloader": "version-bootloader",
    "baseband": "version-baseband", "unlocked": "unlocked", "secure": "secure",
    "slot": "current-slot", "hardware_revision": "hw-revision",
    "battery_mv": "battery-voltage",
}


def listed(tool, state):
    """attached(), but an uninstalled tool simply lists no devices."""
    try:
        return attached(tool, state)
    except (OSError, ValueError):
        return []


def diagnose_mode(args):
    """"adb" or "fastboot": where the requested (or only) phone is."""
    if args.fastboot:
        return "fastboot"
    adb = listed(args.adb, "device")
    if args.serial:
        if args.serial in adb:
            return "adb"
        return "fastboot" if args.serial in listed(args.fastboot_bin, "fastboot") else "adb"
    if adb:
        return "adb"
    return "fastboot" if listed(args.fastboot_bin, "fastboot") else "adb"


def diagnose_fastboot(args):
    """What the bootloader can tell: no logs or properties, but enough to see
    a missing baseband, a locked bootloader or the wrong model."""
    phone = Fastboot(args.serial, args.fastboot_bin)
    variables = phone.getvar_all()
    config = phone.oem_config()
    gsc_version = phone.gsc_version()
    gsc_headers = phone.gsc_headers()
    gsc_avb = phone.gsc_avb_state()
    report = {"serial": phone.serial, "mode": "fastboot"}
    for name, var in FASTBOOT_VARS.items():
        # Not every bootloader lists everything under "all"; ask for the rest.
        report[name] = variables.get(var) or phone.getvar(var)
    parsed = structure_getvar(variables)
    if gsc_version:
        gsc = parsed["variables"].setdefault("gsc", {})
        if isinstance(gsc, dict):
            gsc["firmware_version"] = gsc_version
    if gsc_headers:
        gsc = parsed["variables"].setdefault("gsc", {})
        if isinstance(gsc, dict):
            gsc["firmware_headers"] = gsc_headers
    if gsc_avb:
        gsc = parsed["variables"].setdefault("gsc", {})
        if isinstance(gsc, dict):
            gsc["avb_state"] = gsc_avb
    partitions = parsed["partitions"]
    modem_parts = {k: v for k, v in partitions.items() if MODEM_PARTITION_RE.match(k)}
    modem_re = r"baseband|radio|modem|efs|persist|devinfo|imei|sku|carrier|cp[-_]|ril|nv"
    shown = set(FASTBOOT_VARS.values())
    modem_vars = {k: v for k, v in parsed["variables"].items()
                  if re.search(modem_re, k, re.I) and k not in shown}
    modem_config = {k: v for k, v in sorted(config.items()) if re.search(modem_re, k, re.I)}
    slot = report["slot"]
    modem = modem_parts.get("modem") or modem_parts.get("radio")
    unslotted, slot_groups = regroup(partitions, parsed["slots"])
    report["modem"] = {
        "baseband": report["baseband"],
        # Slotted modem partitions show each slot's copy (with its vbmeta).
        "partitions": {k: (unslotted[k] if k in unslotted else
                           {"slots": {s: slot_groups.get(s, {}).get("partitions", {}).get(k, f)
                                      for s, f in sorted(v.get("slots", {}).items())}})
                       for k, v in modem_parts.items()},
        "variables": modem_vars,
        "oem_config": modem_config,
    }
    report["findings"] = {
        "baseband_missing": not report["baseband"],
        "bootloader_locked": report["unlocked"] == "no",
        "supported_model": report["product"] == "komodo",
        "modem_partitions_missing": sorted(
            p for p in ("efs", "efs_backup", "persist", "devinfo") if p not in partitions)
            if partitions else "bootloader did not list partitions",
        "empty_modem_partitions": sorted(
            p for p, e in modem_parts.items() if 0 in partition_sizes(e)),
        "secure_boot_debug_relaxed": sorted(
            name for name, entry in partitions.items() if entry.get("sbdp") is True) + sorted(
            item for item, v in (parsed["variables"].get("sbdp") or {}).items() if v is True),
        "active_slot_modem_listed": (slot in modem.get("slots", {}) if "slots" in modem else True)
            if slot and modem else None,
        "titan_running_matches_getvar": gsc_running_matches(parsed["variables"].get("gsc")),
        "titan_headers_match_versions": gsc_headers_match_versions(parsed["variables"].get("gsc")),
    }
    # Everything else, without repeating what the fields above already show.
    report["partitions"] = {k: v for k, v in unslotted.items() if k not in modem_parts}
    report["_all_partitions"] = partitions
    report["slots"] = slot_groups
    report["variables"] = {k: v for k, v in parsed["variables"].items()
                           if k not in shown and k not in modem_vars}
    report["oem_config"] = ({k: v for k, v in sorted(config.items()) if k not in modem_config}
                            if config else "not supported by this bootloader")
    report["sign_in"] = "not available in fastboot; boot Android and run diagnose again"
    report["compatibility"] = "devinfo workflow limited to komodo; Android 17 unverified"
    return report


def fbpk_entries(data):
    """Partition names inside an FBPK v2 container (bootloader-*.img, and the
    radio image on Tensor Pixels); None when `data` is not one."""
    if len(data) < 112 or data[:4] != b"FBPK" or int.from_bytes(data[4:8], "little") != 2:
        return None
    count = int.from_bytes(data[104:108], "little")
    names = []
    for i in range(count):
        base = 112 + i * 104
        if base + 104 > len(data):
            fail("Truncated FBPK entry table")
        if int.from_bytes(data[base:base+4], "little") == 0:
            continue  # partition table, not a partition
        names.append(data[base+4:base+40].split(b"\0")[0].decode("ascii", "replace"))
    return names


def strip_slot(name):
    return re.sub(r"_[ab]$", "", name)


def package_partitions(path):
    """{partition: source} for everything a factory image ZIP would write:
    the bootloader and radio packs' entries and the images in image-*.zip."""
    found = {}
    with zipfile.ZipFile(path) as outer:
        for name in outer.namelist():
            base = Path(name).name
            if base.endswith(".img") and base.startswith(("bootloader-", "radio-")):
                kind = base.split("-")[0]
                entries = fbpk_entries(outer.read(name))
                for entry in entries if entries is not None else (["modem"] if kind == "radio" else []):
                    found.setdefault(strip_slot(entry.lower()), base)
            elif base.startswith("image-") and base.endswith(".zip"):
                with outer.open(name) as handle, zipfile.ZipFile(handle) as inner:
                    for member in inner.namelist():
                        if member.endswith(".img"):
                            found.setdefault(strip_slot(Path(member).stem.lower()), base)
    if not found:
        fail(f"{Path(path).name} holds no bootloader, radio or image ZIP; is it a factory image?")
    return dict(sorted(found.items()))


# Partitions that are unique to one phone and therefore never in a factory
# image; losing them is what makes an IMEI unrecoverable without a backup.
DEVICE_UNIQUE = {"devinfo", "efs", "efs_backup", "persist", "modem_userdata", "frp", "misc",
                 "metadata", "userdata", "fips", "dram_train", "klog", "mfg_data"}


def package_coverage(partitions, package):
    """Which of the phone's partitions the restore package rewrites."""
    phone = set(partitions)
    pkg = set(package)
    if "super_empty" in pkg:
        # super is rebuilt from super_empty plus the logical images.
        package = dict(package, super=package["super_empty"])
        pkg.add("super")
    missing = sorted(phone - pkg)
    return {
        "included": {p: package[p] for p in sorted(phone & pkg)},
        "not_included": {
            "device_unique": [p for p in missing if p in DEVICE_UNIQUE],
            "other": [p for p in missing if p not in DEVICE_UNIQUE],
        },
        # Usually logical partitions inside super, which fastboot (not
        # fastbootd) does not list.
        "package_only": sorted(pkg - phone),
    }


def mask(imei, show=False):
    """An IMEI for a report: the whole value only when asked for."""
    if imei is None:
        return None
    return imei if show else "*" * (len(imei) - 4) + imei[-4:]


def devinfo_imeis(data):
    """{"imei1": str, "imei2": str} read from the named devinfo records."""
    return {k: data[o:o+15].decode("ascii", "replace") for k, o in imei_offsets(data).items()}


def imei_encodings(imei):
    """Byte forms an IMEI is commonly stored in: ASCII, UTF-16LE, TBCD (swapped
    nibbles, F pad) and the 3GPP TS 24.008 mobile identity (type IMEI, odd)."""
    d = [int(c) for c in imei]
    tbcd = bytes((d[i+1] if i + 1 < len(d) else 0xF) << 4 | d[i] for i in range(0, len(d), 2))
    ident = bytes([d[0] << 4 | 0xA] + [d[i+1] << 4 | d[i] for i in range(1, len(d), 2)])
    return {"ascii": imei.encode(), "utf16le": imei.encode("utf-16-le"), "tbcd": tbcd, "3gpp": ident}


def ext4_info(data):
    """Superblock facts if `data` is an ext2/3/4 image, else None."""
    sb = data[1024:2048]
    if len(sb) < 1024 or int.from_bytes(sb[0x38:0x3A], "little") != 0xEF53:
        return None
    block = 1024 << int.from_bytes(sb[0x18:0x1C], "little")
    return {
        "label": sb[0x78:0x88].rstrip(b"\0").decode("ascii", "replace"),
        "uuid": sb[0x68:0x78].hex(),
        "blocks": int.from_bytes(sb[0x4:0x8], "little"),
        "block_size": block,
        "clean": int.from_bytes(sb[0x3A:0x3C], "little") & 1 == 1,
        "last_write": int.from_bytes(sb[0x30:0x34], "little"),
        "mount_count": int.from_bytes(sb[0x34:0x36], "little"),
    }


def image_summary(data):
    blank = None
    if data and data.count(data[:1]) == len(data):
        blank = f"every byte is {data[0]:#04x}"
    fs = ext4_info(data)
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "blank": blank, "format": "ext4" if fs else ("unknown" if not blank else "blank"),
            "ext4": fs}


def luhn_numbers(data):
    """Distinct 15-digit ASCII numbers with a valid IMEI check digit."""
    return sorted({m.decode() for m in re.findall(rb"(?<![0-9])[0-9]{15}(?![0-9])", data)
                   if valid_imei(m.decode())})


def inspect_backup(folder, oem_config=None, show=False):
    """Parse a verified backup and check the identity values agree with each
    other (and with the bootloader's oem config, when given). Read-only."""
    folder = Path(folder)
    manifest = check_backup(folder)
    images = {p: (folder / (p + ".img")).read_bytes() for p in PARTITIONS}
    report = {"device": manifest["device"].get("serial"), "partitions": {}}
    for name, data in images.items():
        report["partitions"][name] = image_summary(data)
    cpsha = (folder / "cpsha.bin").read_bytes()
    report["cpsha"] = {"size": len(cpsha), "sha256": hashlib.sha256(cpsha).hexdigest()}
    persist_has_cpsha = cpsha in images["persist"]

    checks = {}
    try:
        imeis = devinfo_imeis(images["devinfo"])
    except ValueError as exc:
        imeis = {}
        checks["devinfo_parsed"] = str(exc)
    else:
        checks["devinfo_parsed"] = True
    report["devinfo"] = {k: {"imei": mask(v, show), "valid": valid_imei(v)} for k, v in imeis.items()}
    if imeis:
        checks["devinfo_imeis_valid"] = all(valid_imei(v) for v in imeis.values())
        checks["devinfo_imeis_distinct"] = imeis.get("imei1") != imeis.get("imei2")
    checks["efs_matches_efs_backup"] = images["efs"] == images["efs_backup"]
    checks["cpsha_found_in_persist"] = persist_has_cpsha

    # Where else do the devinfo IMEIs appear, and in which encoding?
    found = {}
    for key, imei in imeis.items():
        if not valid_imei(imei):
            continue
        for part in ("efs", "efs_backup", "persist"):
            hits = [enc for enc, raw in imei_encodings(imei).items() if raw in images[part]]
            if hits:
                found.setdefault(key, {})[part] = hits
    report["devinfo_imeis_found_in"] = found
    # Other IMEI-shaped numbers stored in plain text, which would disagree.
    others = {part: [mask(n, show) for n in luhn_numbers(images[part]) if n not in imeis.values()]
              for part in ("efs", "efs_backup", "persist")}
    report["other_imei_like_numbers"] = {k: v for k, v in others.items() if v}
    checks["no_conflicting_plain_imeis"] = not report["other_imei_like_numbers"]

    if oem_config is not None:
        oem = {k: v for k, v in oem_config.items() if re.search(r"imei", k, re.I)}
        report["oem_config_imeis"] = {k: {"imei": mask(v, show), "valid": valid_imei(v)} for k, v in oem.items()}
        if oem and imeis:
            oem_values = {v for v in oem.values() if v}
            checks["oem_config_matches_devinfo"] = oem_values == set(imeis.values())
        elif not oem:
            checks["oem_config_matches_devinfo"] = "bootloader config lists no IMEI"
    report["checks"] = checks
    report["consistent"] = all(v is True for v in checks.values())
    return report


# Tags at 0x400 of Tensor boot-chain images, naming the stage the boot ROM or
# previous stage asks for (seen on Pixel 8 images; inferred for others).
TENSOR_STAGE_TAGS = {b"APBL", b"EPBL", b"GSA1", b"GSAF", b"ABL\0", b"TZSW", b"LDFW", b"BL31",
                     b"GCF\0", b"BL2\0", b"DPM\0"}


def avb_header(data):
    """Fields of an AVB vbmeta header (libavb's AvbVBMetaImageHeader)."""
    be = lambda o, n: int.from_bytes(data[o:o+n], "big")
    return {"libavb": f"{be(4, 4)}.{be(8, 4)}", "algorithm": be(28, 4),
            "rollback_index": be(112, 8), "flags": be(120, 4),
            "rollback_index_location": be(124, 4),
            "release": data[128:176].split(b"\0")[0].decode("ascii", "replace")}


def identify(data, show=False):
    """(format, details) for a partition image, from its magic numbers. Read-only."""
    le = lambda o, n: int.from_bytes(data[o:o+n], "little")
    be = lambda o, n: int.from_bytes(data[o:o+n], "big")
    if not data:
        return "empty", {}
    if data.count(data[:1]) == len(data):
        return "blank", {"byte": f"{data[0]:#04x}"}
    footer = {}
    if len(data) >= 64 and data[-64:-60] == b"AVBf":
        # vbmeta appended to the image (boot, vendor_boot, dtbo ...).
        footer = {"avb_footer": {"image_size": be(len(data) - 52, 8),
                                 "vbmeta_offset": be(len(data) - 44, 8)}}
        offset = footer["avb_footer"]["vbmeta_offset"]
        if data[offset:offset+4] == b"AVB0":
            footer["avb_footer"]["vbmeta"] = avb_header(data[offset:])
    if data[:8] == b"ANDROID!":
        return "android_boot_image", dict(header_version=le(40, 4), **footer)
    if data[:8] == b"VNDRBOOT":
        return "vendor_boot_image", dict(header_version=le(8, 4), **footer)
    if data[:4] == b"AVB0":
        return "vbmeta", avb_header(data)
    if data[:4] == b"DEVI":
        try:
            imeis = devinfo_imeis(data)
        except ValueError as exc:
            return "devinfo", {"error": str(exc)}
        return "devinfo", {k: {"imei": mask(v, show), "valid": valid_imei(v)} for k, v in imeis.items()}
    if data[:4] == b"FBPK":
        entries = fbpk_entries(data)
        return "fbpk", {"entries": entries} if entries is not None else {"version": le(4, 4)}
    if le(0, 4) == 0xED26FF3A:
        return "android_sparse", {"blocks": le(16, 4), "block_size": le(12, 4)}
    if be(0, 4) == 0xD7B7AB1E:
        return "dt_table", dict(entries=be(16, 4), **footer)
    if be(0, 4) == 0xD00DFEED:
        return "device_tree", dict(size=be(4, 4), **footer)
    if data[:4] == b"\x7fELF":
        return "elf", {"class": {1: "32-bit", 2: "64-bit"}.get(data[4], data[4])}
    if data[512:520] == b"EFI PART":
        return "gpt", {}
    fs = ext4_info(data)
    if fs:
        return "ext4", fs
    if le(1024, 4) == 0xF2F52010:
        return "f2fs", {}
    if le(1024, 4) == 0xE0F5E1E2:
        return "erofs", {}
    if le(4096, 4) == 0x616C4467:
        return "super", {"metadata_max_size": le(4096 + 40, 4), "slots": le(4096 + 44, 4)}
    if data[:2] == b"\x1f\x8b":
        return "gzip", footer
    if data[:4] in (b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18"):
        return "lz4", footer
    if data[0x400:0x404] in TENSOR_STAGE_TAGS:
        return "tensor_boot_stage", {"tag": data[0x400:0x404].rstrip(b"\0").decode()}
    if footer:
        return "avb_signed_image", footer
    used = len(data.rstrip(data[-1:]))
    return "unknown", {"head": data[:16].hex(), "used_bytes": used}


def parse_size(text):
    """"100M" and the like as bytes."""
    match = re.fullmatch(r"(\d+)\s*([KMG]?)i?B?", text.strip(), re.I)
    if not match:
        raise argparse.ArgumentTypeError(f"not a size: {text}")
    return int(match[1]) << {"": 0, "K": 10, "M": 20, "G": 30}[match[2].upper()]


def fastboot_partition_list(variables):
    """{label: (partition, slot or None, size)} from `getvar all`; the label
    is the on-disk name (boot_a), the partition and slot what fetch takes."""
    found = {}
    for name, entry in structure_getvar(variables)["partitions"].items():
        if "slots" in entry:
            for slot, fields in entry["slots"].items():
                found[f"{name}_{slot}"] = (name, slot, fields.get("size"))
        else:
            found[name] = (name, None, entry.get("size"))
    return found


def summarize_read(images, failed, skipped, max_size, show=False):
    """The read_partitions report for images already on disk."""
    report = {"max_size": max_size, "partitions": {}, "unreadable": failed,
              "skipped_over_limit": sorted(skipped)}
    groups = {"parsed": [], "blank": [], "unknown": []}
    for name, path in sorted(images.items()):
        data = Path(path).read_bytes()
        kind, details = identify(data, show)
        entry = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "format": kind}
        if details:
            entry["details"] = details
        imeis = luhn_numbers(data)
        if imeis:
            entry["imei_like_numbers"] = [mask(n, show) for n in imeis]
        report["partitions"][name] = entry
        groups["blank" if kind in ("blank", "empty") else "unknown" if kind == "unknown" else "parsed"].append(name)
    report["summary"] = dict(groups, unreadable=sorted(failed))
    reasons = {}
    for reason in failed.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    report["summary"]["unreadable_reasons"] = reasons
    return report


def read_partitions(args):
    """Read every partition up to --max-size into a new private folder and say
    which ones parse. Read-only on the phone: adb needs root, fastboot needs a
    bootloader (usually fastbootd) that implements `fetch`."""
    out = private_dir(args.out)
    images, failed, skipped = {}, {}, set()
    if diagnose_mode(args) == "fastboot":
        phone = Fastboot(args.serial, args.fastboot_bin)
        mode, serial = "fastboot", phone.serial
        userspace, unlocked = phone.getvar("is-userspace"), phone.getvar("unlocked")
        for name, (partition, slot, size) in sorted(fastboot_partition_list(phone.getvar_all()).items()):
            if size is None:
                failed[name] = "size unknown"
                continue
            if size > args.max_size:
                skipped.add(name)
                continue
            path = out / (name + ".img")
            error = phone.fetch(partition, path, slot)
            if error:
                failed[name] = error
            else:
                images[name] = path
            print(f"{name}: {error or 'read'}", file=sys.stderr)
    else:
        device = Device(args.serial, args.adb)
        device.require_root()
        mode, serial = "adb", device.serial
        names = device.root("ls /dev/block/by-name").decode().split()
        for name in sorted(n for n in names if re.fullmatch(r"[A-Za-z0-9_.-]+", n)):
            try:
                size = int(device.root("blockdev --getsize64 /dev/block/by-name/" + name).strip())
            except ValueError as exc:
                failed[name] = str(exc)
                continue
            if size > args.max_size:
                skipped.add(name)
                continue
            path = out / (name + ".img")
            try:
                with path.open("xb") as handle:
                    device.root("dd if=/dev/block/by-name/" + name + " bs=1048576", timeout=600, stdout=handle)
            except ValueError as exc:
                failed[name] = str(exc)
                path.unlink(missing_ok=True)
                continue
            images[name] = path
            print(f"{name}: read", file=sys.stderr)
    report = dict(serial=serial, mode=mode, folder=str(out),
                  **summarize_read(images, failed, skipped, args.max_size, args.show_identifiers))
    if mode == "fastboot":
        report["userspace"] = userspace
        report["unlocked"] = unlocked
        if not images and failed:
            report["hint"] = (
                "The bootloader cannot read partitions back; reboot to fastbootd with "
                "`fastboot reboot fastboot` and run this again" if userspace != "yes" else
                "fastbootd refused every read; it only allows fetch on some partitions"
                + ("" if unlocked == "yes" else ", and not while the bootloader is locked (inferred)"))
    write_json(out / "report.json", report)
    emit(report, argparse.Namespace(out=None, json=args.json))


FRIENDLY = {
    # Partitions
    "devinfo": "Manufacturer Device Info", "efs": "Encrypted File Storage",
    "efs_backup": "Encrypted File Storage (backup)", "persist": "Persistent Calibration Data",
    "modem": "Modem Firmware", "radio": "Radio Firmware", "modem_userdata": "Modem Runtime Data",
    "abl": "Android Bootloader", "bl1": "First-Stage Bootloader", "bl2": "Second-Stage Bootloader",
    "bl31": "Secure Monitor (BL31)", "pbl": "Primary Bootloader", "tzsw": "TrustZone Software",
    "sbdp": "Secure Boot Debug Policy (inferred)", "ar-check": "Anti-Rollback Check",
    "ar-update": "Anti-Rollback Update", "pkhash": "Public Key Hash", "allow": "Allow",
    "hv": "Hypervisor", "ta": "Trusted Applications", "tpu": "Tensor Processing Unit",
    "aoc": "Always-On Compute", "tzprov": "TrustZone Provisioning",
    "secure_boot_debug_relaxed": "Debug Policy Relaxed For",
    "anti_rollback": "Anti-Rollback Level", "ap": "Application Processor",
    "anti_rollback_s": "Anti-Rollback Level (Secure)", "anti_rollback_ns": "Anti-Rollback Level (Non-Secure)", "bl1_anti_rollback": "BL1 Anti-Rollback Level",
    "fw_anti_rollback": "Firmware Anti-Rollback Level", "flavor": "Chip Flavor", "chip": "Chip", "firmware_version": "Firmware Version", "running": "Running",
    "build_id": "Build ID", "commit": "Commit", "state": "State", "image_version": "Image Version", "build": "Build",
    "magic": "Magic", "signed_for": "Signed For", "keyid": "Signing Key ID", "tag": "Tag", "text": "Text",
    "utc": "UTC", "img_size": "Image Size", "ro_base": "Base Address", "epoch": "Epoch",
    "major": "Major", "minor": "Minor", "img_chk": "Image Checksum", "fuses_chk": "Fuses Checksum",
    "info_chk": "Info Checksum", "applysec": "Apply Security", "config1": "Config 1",
    "err_response": "Error Response", "expect_response": "Expected Response",
    "fusemap": "Fuse Map", "fuses_checked": "Fuses Checked", "infomap": "Info Map",
    "cleared_bits": "Cleared Bits", "rollback_level": "Rollback Level (inferred)",
    "board_id": "Board ID", "any_board": "Runs On Any Board", "mask": "Mask", "flags": "Flags",
    "value": "Value", "timestamp": "Built",
    "avb_app_version": "AVB App Version", "in_bootloader": "Bootloader Window Open",
    "production": "Production Mode", "lock_count": "Number of Locks", "locks": "Locks",
    "carrier_lock": "Carrier Lock", "device_lock": "Device Lock", "boot_lock": "Boot Lock",
    "owner_lock": "Owner Lock",
    "locked": "Locked", "index": "Index",
    "firmware_headers": "Firmware Headers", "avb_state": "Verified Boot State", "ro_a": "Read-Only Image A", "ro_b": "Read-Only Image B",
    "rw_a": "Read-Write Image A", "rw_b": "Read-Write Image B",
    "ro": "Read-Only Firmware", "rw": "Read-Write Firmware",
    "dsn": "Device Serial Number", "bt_addr": "Bluetooth MAC Address", "coo": "Country of Origin",
    "ddr-manu": "DDR Manufacturer",
    "ldfw": "Loadable Firmware", "gsc": "Titan M", "gsa": "Google Security Applet",
    "gsa_bl1": "Google Security Applet Bootloader", "boot": "Kernel Boot Image", "init_boot": "Generic Ramdisk",
    "vendor_boot": "Vendor Boot Image", "vendor_kernel_boot": "Vendor Kernel Boot Image",
    "dtbo": "Device Tree Overlays", "vbmeta": "Verified Boot Metadata",
    "vbmeta_system": "Verified Boot Metadata (system)", "vbmeta_vendor": "Verified Boot Metadata (vendor)",
    "pvmfw": "Protected VM Firmware", "super": "Dynamic Partitions", "userdata": "User Data",
    "metadata": "Encryption Metadata", "misc": "Boot Control Messages", "frp": "Factory Reset Protection",
    # Bootloader variables
    "product": "Product", "bootloader": "Bootloader Version", "baseband": "Baseband Version",
    "version-bootloader": "Bootloader Version", "version-baseband": "Baseband Version",
    "unlocked": "Bootloader Unlocked", "secure": "Secure Boot", "slot": "Active Slot",
    "current-slot": "Active Slot", "hardware_revision": "Hardware Revision", "hw-revision": "Hardware Revision",
    "battery_mv": "Battery (mV)", "battery-voltage": "Battery (mV)", "serialno": "Serial Number",
    "max-download-size": "Max Download Size", "slot-count": "Slot Count", "variant": "Variant",
    "off-mode-charge": "Charge When Off", "version": "Fastboot Protocol",
    # Report sections and fields
    "serial": "Serial Number", "mode": "Connection", "findings": "Findings", "partitions": "Partitions",
    "slots": "Slots", "vbmeta": "Verified Boot Metadata", "variables": "Other Variables", "oem_config": "OEM Config",
    "restore_package": "Restore Package", "included": "Restored From Package",
    "not_included": "Not In Package", "device_unique": "Unique To This Phone", "other": "Other",
    "package_only": "Only In Package", "backup_check": "Backup Check", "checks": "Checks",
    "consistent": "Consistent", "devinfo_imeis_found_in": "Devinfo IMEIs Found In",
    "other_imei_like_numbers": "Other IMEI-Like Numbers", "oem_config_imeis": "IMEIs In OEM Config",
    "folder": "Saved To", "max_size": "Size Limit", "unreadable": "Could Not Read",
    "skipped_over_limit": "Over Size Limit", "summary": "Summary", "parsed": "Parsed",
    "unknown": "Unrecognized", "details": "Details", "header_version": "Header Version",
    "imei_like_numbers": "IMEI-Like Numbers", "avb_footer": "Verified Boot Footer",
    "rollback_index": "Rollback Index", "userspace": "Userspace Fastboot (fastbootd)",
    "hint": "Hint", "unreadable_reasons": "Why Reads Failed",
    "cpsha": "Modem Identity Hash (cpsha)", "size": "Size", "type": "Type", "logical": "Logical",
    "successful": "Boot Successful", "fastboot_ok": "Fastboot OK", "unbootable": "Unbootable", "retry_count": "Retries Left",
    "sign_in": "Google Sign-In", "compatibility": "Compatibility", "imei": "IMEI", "valid": "Valid",
    "sha256": "SHA-256", "blank": "Blank", "format": "Format", "file": "File",
    # Findings and checks
    "baseband_missing": "Baseband Missing", "bootloader_locked": "Bootloader Locked",
    "supported_model": "Supported Model", "modem_partitions_missing": "Modem Partitions Missing",
    "empty_modem_partitions": "Empty Modem Partitions", "active_slot_modem_listed": "Active Slot Has Modem",
    "devinfo_parsed": "Devinfo Parsed", "devinfo_imeis_valid": "Devinfo IMEIs Valid",
    "devinfo_imeis_distinct": "Devinfo IMEIs Distinct", "efs_matches_efs_backup": "EFS Matches Its Backup",
    "cpsha_found_in_persist": "cpsha Present In Persist",
    "no_conflicting_plain_imeis": "No Conflicting IMEIs", "oem_config_matches_devinfo": "OEM Config Matches Devinfo",
}
# Names for the report's own sections, where a key doubles as a partition name.
SECTIONS = {"modem": "Modem"}
# The value a finding should have; anything else is highlighted as a problem.
EXPECTED = {
    "baseband_missing": False, "bootloader_locked": False, "supported_model": True,
    "modem_partitions_missing": [], "empty_modem_partitions": [], "active_slot_modem_listed": True,
    "consistent": True, "no_conflicting_plain_imeis": True, "valid": True,
    "secure_boot_debug_relaxed": [],
}
COLORS = {"key": "36", "dim": "2", "good": "32", "bad": "1;31", "value": "0"}


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:g} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024


def render_tree(report, color=True):
    """The report as an indented tree with friendly names, the raw key in
    parentheses, and problems highlighted."""
    def paint(text, kind):
        return f"\033[{COLORS[kind]}m{text}\033[0m" if color else text

    def label(key, parent=None):
        name = (SECTIONS.get(key) if parent is None else None) or FRIENDLY.get(key)
        return paint(name, "key") + " " + paint(f"({key})", "dim") if name else paint(key, "key")

    def verdict(key, value, parent):
        if key in EXPECTED:
            return "good" if value == EXPECTED[key] else "bad"
        if parent == "checks":
            return "good" if value is True else "bad"
        return None

    def scalar(key, value, parent):
        state = verdict(key, value, parent)
        if value is None:
            text = paint("unknown", "dim")
        elif isinstance(value, bool):
            text = "yes" if value else "no"
        elif key == "size" and isinstance(value, int):
            text = f"{human_size(value)} ({value:#x})"
        elif isinstance(value, list):
            text = ", ".join(FRIENDLY.get(v, v) if isinstance(v, str) else str(v) for v in value) or "none"
        else:
            text = str(value)
        return paint(text, state) if state else (text if value is not None else text)

    slot_info = report.get("slots") if isinstance(report.get("slots"), dict) else {}
    active = report.get("slot")

    def slot_tags(slot):
        """active / booted / fastboot ok (and unbootable), coloured."""
        info = slot_info.get(slot, {})
        tags = []
        if slot == active:
            tags.append(paint("active", "key"))
        if info.get("successful") is True:
            tags.append(paint("booted", "good"))
        if info.get("fastboot_ok") is True:
            tags.append(paint("fastboot ok", "good"))
        elif info.get("fastboot_ok") is False:
            tags.append(paint("no fastboot", "dim"))
        if info.get("unbootable") is True:
            tags.append(paint("unbootable", "bad"))
        if "retry_count" in info:
            tags.append(paint(f"{info['retry_count']} retries left", "dim"))
        return tags

    def slot_label(slot):
        tags = slot_tags(slot)
        return paint(f"Slot {slot}", "key") + (" " + paint("[", "dim") + ", ".join(tags)
                                               + paint("]", "dim") if tags else "")

    lines = []

    def walk(node, prefix, parent, depth=0):
        items = list(node.items())
        for i, (key, value) in enumerate(items):
            last = i == len(items) - 1
            branch = paint("└─ " if last else "├─ ", "dim")
            if parent == "slots" and key in ("a", "b"):
                if depth == 1 and isinstance(value, dict) and "partitions" in value:
                    # A slot: its state as tags, then the partitions in it.
                    lines.append(prefix + branch + slot_label(key))
                    walk(value["partitions"], prefix + paint("   " if last else "│  ", "dim"),
                         "slot-partitions", depth + 1)
                    continue
                if depth == 1 or not (isinstance(value, dict) and set(value) - {"size"}):
                    # The top-level slot list is fully described by its tags;
                    # a partition's slot shows its tags and size.
                    size = value.get("size") if isinstance(value, dict) else None
                    lines.append(prefix + branch + slot_label(key)
                                 + (": " + scalar("size", size, parent) if size is not None else ""))
                    continue
                lines.append(prefix + branch + slot_label(key))
                walk(value, prefix + paint("   " if last else "│  ", "dim"), key, depth + 1)
                continue
            if isinstance(value, dict) and set(value) == {"size"}:
                # A partition (or slot) known only by its size: one line.
                lines.append(prefix + branch + label(key, parent) + ": " + scalar("size", value["size"], parent))
            elif isinstance(value, dict) and value:
                lines.append(prefix + branch + label(key, parent))
                walk(value, prefix + paint("   " if last else "│  ", "dim"), key, depth + 1)
            else:
                text = paint("none", "dim") if value == {} else scalar(key, value, parent)
                lines.append(prefix + branch + label(key, parent) + ": " + text)

    title = f"Pixel {report.get('product') or report.get('model') or ''} {report.get('serial', '')}".strip()
    lines.append(paint(title, "key"))
    walk(report, "", None)
    return "\n".join(lines)


def emit(report, args):
    if args.out:
        write_json(args.out, report)
    if getattr(args, "json", False) or not sys.stdout.isatty():
        print(json.dumps(report, indent=2))
    else:
        print(render_tree(report, color="NO_COLOR" not in os.environ))


def diagnose(args):
    pull = getattr(args, "pull", None)
    backup_dir = getattr(args, "backup", None)
    show = getattr(args, "show_identifiers", False)
    if diagnose_mode(args) == "fastboot":
        if pull:
            fail("Fastboot cannot read partitions; --pull needs Android with root. "
                 "Use --backup DIR to check an existing backup against the bootloader")
        report = diagnose_fastboot(args)
        all_partitions = report.pop("_all_partitions")
        package = getattr(args, "package", None)
        if package:
            report["restore_package"] = dict(file=Path(package).name,
                                             **package_coverage(all_partitions, package_partitions(package)))
        if backup_dir:
            config = report["oem_config"] if isinstance(report["oem_config"], dict) else {}
            report["backup_check"] = inspect_backup(backup_dir, config, show)
        emit(report, args)
        return
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
    if pull:
        backup(argparse.Namespace(serial=device.serial, adb=args.adb, out=pull))
        backup_dir = pull
    if backup_dir:
        report["backup_check"] = inspect_backup(backup_dir, None, show)
    emit(report, args)


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
    if args.serial and args.serial != plan["device"]["serial"]:
        fail("Selected serial differs from repair plan")
    image = Path(args.plan).resolve() / "devinfo-restored.img"
    if args.fastboot:
        return apply_fastboot(args, plan, image)
    device = Device(args.serial, args.adb)
    if device.serial != plan["device"]["serial"]:
        fail("Attached device serial differs from repair plan")
    if device.info() != plan["device"]:
        fail("Device properties changed since backup; make a fresh backup and plan")
    device.require_root()
    if device.digest("devinfo") != plan["original_sha256"]:
        fail("Live devinfo changed since backup; refusing a stale plan")
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


def apply_fastboot(args, plan, image):
    """Write the prepared devinfo from the bootloader, for a phone that cannot
    boot Android with root. The live partition cannot be read here, so the
    stale-plan check the adb path makes is replaced by explicit consent."""
    if not args.no_readback:
        fail("Fastboot cannot read devinfo back to check the plan is current or verify "
             "the write; pass --no-readback to accept that, after a fresh backup and plan")
    phone = Fastboot(args.serial, args.fastboot_bin)
    if phone.serial != plan["device"]["serial"]:
        fail("Phone in fastboot is not the device the plan was made for")
    phone.require(plan["device"]["model"])
    size = phone.getvar("partition-size:devinfo")
    if size is not None and int(size, 16) != image.stat().st_size:
        fail(f"devinfo partition is {int(size, 16)} bytes; prepared image is {image.stat().st_size}")
    print("Flashing devinfo only; do not disconnect power or USB.", flush=True)
    phone.call("flash", "devinfo", str(image))
    print("devinfo flashed (fastboot cannot read it back). Reboot manually, "
          "then retry sign-in and run diagnose.")


def radio_image(path, out):
    """radio-*.img itself, or extracted from a factory ZIP into `out`."""
    path = Path(path)
    if path.suffix.lower() != ".zip":
        return path
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist()
                 if Path(n).name.startswith("radio-") and n.endswith(".img")]
        if len(names) != 1:
            fail(f"Expected one radio-*.img in {path.name}, found {len(names)}")
        target = Path(out) / Path(names[0]).name
        if target.exists():
            fail(f"{target} already exists")
        Path(out).mkdir(parents=True, exist_ok=True)
        with archive.open(names[0]) as src, target.open("xb") as dst:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(block)
    print(f"Extracted {target}")
    return target


def reflash_radio(args):
    """Rewrite the modem firmware from the bootloader. Firmware only: the
    phone's identity lives in devinfo/efs/persist, which this never touches."""
    image = radio_image(args.image, args.out)
    match = re.fullmatch(r"radio-([a-z0-9]+)-(.+)\.img", image.name)
    if not match:
        fail(f"{image.name} is not named like radio-<device>-<version>.img")
    product, version = match.groups()
    phone = Fastboot(args.serial, args.fastboot_bin)
    phone.require(product)
    before = phone.getvar("version-baseband")
    print(f"{phone.serial}: {product}, bootloader {phone.getvar('version-bootloader')}, "
          f"baseband {before or 'none reported'}")
    print(f"Radio to flash: {image.name} (baseband {version})")
    if not args.apply:
        print("Dry run. To write, pass --apply. Use the radio from the same factory "
              "image as the phone's bootloader.")
        return
    phone.call("flash", "radio", str(image))
    phone.call("reboot-bootloader")
    phone.wait()
    after = phone.getvar("version-baseband")
    print(f"Baseband now {after or 'none reported'}. Reboot manually and run diagnose.")
    if not after or after.lower() != version.lower():
        fail("Bootloader does not report the flashed baseband; check the radio matches the bootloader build")


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
    diag.add_argument("--fastboot", action="store_true",
                      help="Read bootloader variables; the default when the phone is only in fastboot")
    diag.add_argument("--pull", metavar="DIR",
                      help="Back up the modem partitions (adb, root) into a new DIR and check them")
    diag.add_argument("--backup", metavar="DIR",
                      help="Check an existing backup (against the bootloader's config in fastboot)")
    diag.add_argument("--package", metavar="ZIP",
                      help="In fastboot, list which partitions this factory image would and would not restore")
    diag.add_argument("--json", action="store_true",
                      help="Print JSON instead of the tree (the default when not on a terminal)")
    diag.add_argument("--show-identifiers", action="store_true",
                      help="Print whole IMEIs instead of the last four digits")
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
    restore.add_argument("--fastboot", action="store_true",
                         help="Write from the bootloader instead of a rooted adb shell")
    restore.add_argument("--no-readback", action="store_true",
                         help="With --fastboot, accept that the write cannot be read back")
    restore.set_defaults(func=apply)
    read = sub.add_parser("read-partitions",
                          help="Read every partition up to --max-size and report which ones parse")
    read.add_argument("--out", required=True, help="New private folder for the images and report.json")
    read.add_argument("--max-size", type=parse_size, default=100 << 20, help="Largest partition to read (default 100M)")
    read.add_argument("--fastboot", action="store_true", help="Read with `fastboot fetch` instead of adb")
    read.add_argument("--json", action="store_true", help="Print JSON instead of the tree")
    read.add_argument("--show-identifiers", action="store_true", help="Print whole IMEIs")
    read.set_defaults(func=read_partitions)
    radio = sub.add_parser("reflash-radio", help="Preview or flash radio-*.img from fastboot")
    radio.add_argument("--image", required=True, help="radio-*.img or the factory ZIP holding it")
    radio.add_argument("--out", default=".", help="Where to extract the radio from a ZIP")
    radio.add_argument("--apply", action="store_true")
    radio.set_defaults(func=reflash_radio)
    for child in (diag, back, restore, read):
        child.add_argument("--serial", help="Device serial; optional when one device is attached")
        child.add_argument("--adb", default="adb", help="ADB executable")
    for child in (diag, restore, radio, read):
        if child is radio:
            child.add_argument("--serial", help="fastboot serial; optional when one device is attached")
        child.add_argument("--fastboot-bin", default="fastboot", help="fastboot executable")
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
