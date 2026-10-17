"""Offline tests for pixel_titan.py. No fastboot, network or phone."""
import argparse
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("pixel_titan", Path(__file__).with_name("pixel_titan.py"))
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

# Rick's komodo, `oem gsc version` as Fastboot.gsc_version() returns it.
VERSION = {"RO_A": "* 0.2.114/9b4e3277 ok", "RO_B": "0.2.112/5cfcbae6 ok",
           "RW_A": "* 0.2.2/nugget_v0.0.ab15203921-02e2916-r ok",
           "RW_B": "0.2.2/nugget_v0.0.ab15116366-6c96681-r ok"}
RW_BASE, RO_BASE = 0x84000, 0x80000


def header(version=(0, 2, 2), level=2, base=RW_BASE, magic=0xFFFFFFFD, size=4096):
    """A Titan M SignedHeader followed by body, `level` infomap bits cleared."""
    h = bytearray(b"\x33" * 1024)
    h[0:4] = magic.to_bytes(4, "little")
    for field, value in ((808, size), (812, base), (816, base + 0x3c000)):
        h[field:field+4] = value.to_bytes(4, "little")
    for i, v in enumerate(version):
        h[860+4*i:864+4*i] = v.to_bytes(4, "little")
    h[844:860] = ((0xFFFFFFFF << level) & 0xFFFFFFFF).to_bytes(4, "little") + b"\xff" * 12
    return bytes(h) + bytes(size - 1024)


def tagged(tag, **kw):
    return header(**kw) + b"\0" * 16 + tag + b"\0"


SAME = header(base=RO_BASE) + tagged(b"nugget_v0.0.ab15203921-02e2916-r") + b"9b4e3277"
NEWER = tagged(b"nugget_v0.0.ab15300000-1234567-r", version=(0, 2, 3))
OLDER = tagged(b"nugget_v0.0.ab15116366-6c96681-r")
# The chip's `oem gsc header` answers as pixel_modem parses them.
CHIP = {"ro_a": {"epoch": 0, "major": 2, "minor": 114, "ro_base": f"{RO_BASE:#010x}",
                 "infomap": {"rollback_level": 0}},
        "rw_a": {"epoch": 0, "major": 2, "minor": 2, "ro_base": f"{RW_BASE:#010x}",
                 "infomap": {"rollback_level": 2}},
        "rw_b": {"epoch": 0, "major": 2, "minor": 2, "ro_base": f"{RW_BASE + 0x40000:#010x}",
                 "infomap": {"rollback_level": 1}}}


def fbpk(entries):
    """A minimal FBPK v2 image holding `entries` ({name: bytes})."""
    table = 112 + 104 * len(entries)
    header = bytearray(112)
    header[0:4] = b"FBPK"
    header[4:8] = (2).to_bytes(4, "little")
    header[104:108] = len(entries).to_bytes(4, "little")
    rows, payload, offset = b"", b"", table
    for name, data in entries.items():
        row = bytearray(104)
        row[0:4] = (1).to_bytes(4, "little")
        row[4:4+len(name)] = name.encode()
        row[80:88] = offset.to_bytes(8, "little")
        row[88:96] = len(data).to_bytes(8, "little")
        rows += bytes(row)
        payload += data
        offset += len(data)
    return bytes(header) + rows + payload


class Running(unittest.TestCase):
    def test_parses_ricks_chip(self):
        r = t.parse_running(VERSION)
        self.assertEqual(r["rw_a"], {"booted": True, "version": "0.2.2", "state": "ok",
                                     "tag": "nugget_v0.0.ab15203921-02e2916-r",
                                     "build": 15203921, "hash": "02e2916"})
        parsed = {k: t.m.parse_gsc_image(v) for k, v in VERSION.items()}
        self.assertEqual(t.parse_running(parsed), r)
        self.assertEqual(r["ro_a"]["hash"], "9b4e3277")
        self.assertFalse(r["rw_b"]["booted"])

    def test_keeps_unparsed_lines(self):
        self.assertEqual(t.parse_running({"note": "busy"}), {"note": {"raw": "busy"}})
        self.assertEqual(t.parse_running(["line"]), {})


class Rollback(unittest.TestCase):
    running = t.parse_running(VERSION)

    def test_same_build_is_allowed(self):
        check = t.rollback_check(self.running, t.image_builds(SAME))
        self.assertTrue(check["same_as_booted"])
        self.assertEqual(check["floor"]["build"], 15203921)

    def test_newer_is_allowed(self):
        self.assertFalse(t.rollback_check(self.running, t.image_builds(NEWER))["same_as_booted"])

    def test_older_is_refused(self):
        with self.assertRaisesRegex(ValueError, "allow-downgrade"):
            t.rollback_check(self.running, t.image_builds(OLDER))

    def test_older_is_allowed_with_the_flag(self):
        self.assertTrue(t.rollback_check(self.running, t.image_builds(OLDER), allow_downgrade=True)["downgrade"])

    def test_any_older_build_in_the_image_is_refused(self):
        with self.assertRaisesRegex(ValueError, "older"):
            t.rollback_check(self.running, t.image_builds(NEWER + OLDER))

    def test_unreadable_image_is_refused(self):
        with self.assertRaisesRegex(ValueError, "No nugget build"):
            t.rollback_check(self.running, t.image_builds(b"\0" * 100))

    def test_no_floor_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no RW build"):
            t.rollback_check({}, t.image_builds(SAME))

    def test_ro_hashes(self):
        self.assertEqual(t.ro_matches(self.running, SAME), {"RO_A": True, "RO_B": False})


class Headers(unittest.TestCase):
    def test_finds_rw_and_ro_headers(self):
        found = t.signed_headers(SAME)
        self.assertEqual([(h["offset"], h["ro_base"], h["version"], h["rollback_level"]) for h in found],
                         [(0, "0x00080000", (0, 2, 2), 2), (4096, "0x00084000", (0, 2, 2), 2)])

    def test_ignores_erased_flash(self):
        self.assertEqual(t.signed_headers(b"\xff" * 8192), [])

    def test_same_header_passes_and_ro_is_not_held_to_rw(self):
        check = t.header_check(CHIP, t.signed_headers(SAME))
        self.assertEqual(check["floor"], {"version": "0.2.2", "rollback_level": 2})

    def test_older_version_is_refused(self):
        with self.assertRaisesRegex(ValueError, "older than RW 0.2.2"):
            t.header_check(CHIP, t.signed_headers(header(version=(0, 2, 1))))
        t.header_check(CHIP, t.signed_headers(header(version=(0, 2, 1))), allow_downgrade=True)

    def test_lower_rollback_level_is_refused_even_as_a_downgrade(self):
        with self.assertRaisesRegex(ValueError, "would not boot"):
            t.header_check(CHIP, t.signed_headers(header(version=(0, 2, 1), level=1)), allow_downgrade=True)

    def test_lower_rollback_level_is_refused(self):
        with self.assertRaisesRegex(ValueError, "would not boot"):
            t.header_check(CHIP, t.signed_headers(header(level=1)))

    def test_higher_rollback_level_needs_the_flag(self):
        image = t.signed_headers(header(version=(0, 2, 3), level=3))
        with self.assertRaisesRegex(ValueError, "cannot be undone"):
            t.header_check(CHIP, image)
        self.assertEqual(t.header_check(CHIP, image, allow_bump=True)["floor"]["rollback_level"], 2)

    def test_no_header_or_no_chip_header_is_refused(self):
        with self.assertRaisesRegex(ValueError, "No Titan M signed header"):
            t.header_check(CHIP, [])
        with self.assertRaisesRegex(ValueError, "no RW header"):
            t.header_check({"rw_a": ["unparsed"]}, t.signed_headers(SAME))

    def test_ro_only_image_is_refused(self):
        with self.assertRaisesRegex(ValueError, "only RO"):
            t.header_check(CHIP, t.signed_headers(header(base=RO_BASE)))


def intel_hex(data, base=0x84000):
    lines = [bytes([2, 0, 0, 4]) + (base >> 16).to_bytes(2, "big")]
    for off in range(0, len(data), 16):
        chunk = data[off:off+16]
        lines.append(bytes([len(chunk)]) + ((base + off) & 0xFFFF).to_bytes(2, "big") + b"\0" + chunk)
    lines.append(bytes([0, 0, 0, 1]))
    return b"\n".join(b":" + (r + bytes([-sum(r) & 0xFF])).hex().upper().encode() for r in lines)


class Rescue(unittest.TestCase):
    def test_intel_hex_is_decoded(self):
        self.assertEqual(t.rescue_payload(intel_hex(NEWER)), NEWER)
        self.assertEqual(t.image_builds(t.rescue_payload(intel_hex(NEWER)))[0]["build"], 15300000)

    def test_raw_is_unchanged(self):
        self.assertEqual(t.rescue_payload(SAME), SAME)

    def test_bad_checksum_is_refused(self):
        bad = intel_hex(NEWER)[:-2] + b"00"
        with self.assertRaisesRegex(ValueError, "malformed"):
            t.rescue_payload(bad)


class Find(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def factory(self, inner_name="d3m2.bin", where="image"):
        path = self.dir / "komodo-cp3a.260905.009-factory.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as inner:
            inner.writestr("vendor.img", b"x")
            if where == "image":
                inner.writestr(f"firmware/{inner_name}", SAME)
        with zipfile.ZipFile(path, "w") as outer:
            outer.writestr("komodo/image-komodo.zip", buf.getvalue())
            entries = {"abl": b"a"}
            if where == "bootloader":
                entries["d3m2"] = SAME
            outer.writestr("komodo/bootloader-komodo-x.img", fbpk(entries))
        return path

    def test_in_nested_image_zip(self):
        data, where, product = t.find_firmware(self.factory(), "d3m2")
        self.assertEqual((data, product), (SAME, "komodo"))
        self.assertIn("image-komodo.zip:firmware/d3m2.bin", where)

    def test_in_bootloader_pack(self):
        data, where, _ = t.find_firmware(self.factory(where="bootloader"), "d3m2")
        self.assertEqual(data, SAME)
        self.assertTrue(where.endswith(":d3m2"))

    def test_prefers_the_rescue_image(self):
        path = self.dir / "komodo-x-factory.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("vendor/firmware/d3m2.bin", NEWER)
            z.writestr("vendor/firmware/d3m2.rec", SAME)
        data, where, _ = t.find_firmware(path, "d3m2")
        self.assertEqual(data, SAME)
        self.assertTrue(where.endswith("d3m2.rec"))

    def test_missing(self):
        with self.assertRaisesRegex(ValueError, "No d3m2.rec or d3m2.bin"):
            t.find_firmware(self.factory(inner_name="other.bin"), "d3m2")

    def test_plain_file_must_name_flavor(self):
        good, bad = self.dir / "d3m2.bin", self.dir / "c1.bin"
        good.write_bytes(SAME)
        bad.write_bytes(SAME)
        self.assertEqual(t.find_firmware(good, "d3m2")[0], SAME)
        with self.assertRaisesRegex(ValueError, "not named"):
            t.find_firmware(bad, "d3m2")
        rec = self.dir / "ec.rec"
        rec.write_bytes(SAME)
        self.assertEqual(t.find_firmware(rec, "d3m2")[0], SAME)

    def test_folder(self):
        (self.dir / "vendor/firmware").mkdir(parents=True)
        (self.dir / "vendor/firmware/d3m2.bin").write_bytes(SAME)
        self.assertEqual(t.find_firmware(self.dir, "d3m2")[0], SAME)


class Writes(unittest.TestCase):
    def test_only_rescue_and_reset(self):
        phone = MagicMock(fastboot="fastboot", serial="X")
        for args in (("avb", "rb", "1"), ("avb", "rb"), ("stage",), ("recover",), ("rescue", "x")):
            with self.assertRaisesRegex(ValueError, "Refusing"):
                t.gsc_write(phone, *args)

    def test_failed_output_raises(self):
        phone = MagicMock(fastboot="fastboot", serial="X")
        done = MagicMock(returncode=0, stdout=b"", stderr=b"FAILED (remote: 'bad image')")
        with patch.object(t.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(ValueError, "bad image"):
                t.gsc_write(phone, "rescue")


class Repair(unittest.TestCase):
    def run_repair(self, image, apply=False, confirm=None, after=None, downgrade=False):
        work = Path(tempfile.mkdtemp())
        firmware = work / "d3m2.bin"
        firmware.write_bytes(image)
        phone = MagicMock(serial="47191FDAS0056M", fastboot="fastboot")
        phone.getvar.side_effect = {"product": "komodo", "gsc-flavor": "d3m2",
                                    "version-bootloader": "ripcurrentpro-17.0"}.get
        phone.gsc_version.side_effect = [VERSION, after or VERSION]
        phone.gsc_headers.return_value = CHIP
        phone.gsc_avb_state.return_value = None
        args = argparse.Namespace(firmware=str(firmware), out=str(work / "out"), apply=apply,
                                  confirm=confirm, allow_rollback_bump=False, allow_downgrade=downgrade, serial=None, fastboot_bin="fastboot")
        sent = []
        with patch.object(t.m, "Fastboot", return_value=phone), \
             patch.object(t, "gsc_write", side_effect=lambda p, *a: sent.append(a) or "OKAY"), \
             redirect_stdout(io.StringIO()) as out:
            t.repair(args)
        return phone, sent, work / "out", out.getvalue()

    def test_dry_run_sends_nothing(self):
        phone, sent, out, text = self.run_repair(SAME)
        self.assertEqual(sent, [])
        phone.call.assert_not_called()
        self.assertEqual((out / "d3m2.bin").read_bytes(), SAME)
        self.assertEqual(json.loads((out / "before.json").read_text())["flavor"], "d3m2")
        self.assertIn("Dry run", text)

    def test_apply_needs_the_serial(self):
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            self.run_repair(SAME, apply=True, confirm="WRONG")

    def test_apply_stages_rescues_then_resets(self):
        phone, sent, out, _ = self.run_repair(SAME, apply=True, confirm="47191FDAS0056M")
        phone.call.assert_called_once_with("stage", str(out / "d3m2.bin"))
        self.assertEqual(sent, [("rescue",), ("reset",)])
        self.assertTrue((out / "after.json").exists())

    def test_apply_reports_bad_state_after(self):
        after = dict(VERSION, RW_A="* 0.2.2/nugget_v0.0.ab15203921-02e2916-r bad")
        with self.assertRaisesRegex(ValueError, "RW_A"):
            self.run_repair(SAME, apply=True, confirm="47191FDAS0056M", after=after)

    def test_downgrade_is_sent_with_the_flag(self):
        _, sent, _, text = self.run_repair(OLDER, apply=True, confirm="47191FDAS0056M", downgrade=True)
        self.assertEqual(sent, [("rescue",), ("reset",)])
        self.assertIn("OLDER (downgrade)", text)

    def test_older_image_never_reaches_the_phone(self):
        with self.assertRaisesRegex(ValueError, "older"):
            self.run_repair(OLDER, apply=True, confirm="47191FDAS0056M")


if __name__ == "__main__":
    unittest.main()
