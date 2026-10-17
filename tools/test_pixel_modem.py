"""Offline regression tests. No ADB, network, or real device access."""
import argparse
import hashlib
import importlib.util
import json
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("pixel_modem", Path(__file__).with_name("pixel_modem.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def record(key, value):
    key = key.encode() + b"\0"
    return b"DIUS" + struct.pack("<II", len(key) + len(value), len(key)) + key + value


def image():
    return (b"DEVI" + bytes(124) + record("serial", b"111111111111111\0") +
            record("imei1", b"FFFFFFFFFFFFFF4\0") +
            record("imei2", b"000000000000000\0") + bytes(64))


GETVAR_ALL = """(bootloader) version-bootloader: ripcurrentpro-17.0-1
(bootloader) version-baseband: g5400c-250909-250916-B-14110587
(bootloader) current-slot: a
(bootloader) hw-revision: MP1.0
(bootloader) partition-size:efs: 0x1000000
(bootloader) partition-type:efs: raw
(bootloader) partition-size:efs_backup: 0x1000000
(bootloader) partition-size:persist: 0x0
(bootloader) partition-size:modem_a: 0x10000000
(bootloader) partition-size:modem_b: 0x10000000
(bootloader) partition-size:boot_a: 0x4000000
(bootloader) partition-size:boot_b: 0x4000000
(bootloader) has-slot:boot: yes
(bootloader) slot-successful:a: yes
(bootloader) slot-retry-count:a: 7
(bootloader) max-download-size: 0x10000000
Finished. Total time: 0.050s
"""
OEM_CONFIG = """(bootloader) sku: GZC4K
(bootloader) modem-boot=ok
(bootloader) fdr_allowed: 1
OKAY [  0.010s]
Finished. Total time: 0.011s
"""
IMEIS = {"imei1": "490154203237518", "imei2": "356938035643809"}
INFO = {"serial": "TEST123", "model": "komodo", "manufacturer": "Google",
        "android": "17", "fingerprint": "google/komodo/test", "bootloader_locked": "0",
        "verified_boot": "orange", "baseband": "test"}


class ModemTests(unittest.TestCase):
    def setUp(self):
        output = patch("sys.stdout", new_callable=io.StringIO)
        output.start()
        self.addCleanup(output.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_backup(self):
        out = self.root / "backup"
        out.mkdir()
        manifest = {"schema": 1, "device": INFO.copy(), "files": {}}
        for name in [p + ".img" for p in m.PARTITIONS] + ["cpsha.bin"]:
            data = image() if name == "devinfo.img" else b"fixture backup data"
            (out / name).write_bytes(data)
            manifest["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        m.write_json(out / "manifest.json", manifest)
        return out

    def make_plan(self):
        backup = self.make_backup()
        identities = self.root / "original.json"
        identities.write_text(json.dumps(dict(IMEIS, serial="TEST123", confirmed_original=True)))
        plan = self.root / "plan"
        m.prepare(argparse.Namespace(backup=backup, original_imeis=identities, out=plan))
        return plan

    def args(self, plan, **kwargs):
        values = dict(plan=plan, apply=True, confirm_original_imeis=True,
                      allow_unverified_build=True, serial="TEST123", adb="adb",
                      fastboot=False, no_readback=False, fastboot_bin="fastboot")
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_imei_validation(self):
        for v in IMEIS.values():
            self.assertTrue(m.valid_imei(v))
        for v in ("FFFFFFFFFFFFFF4", "000000000000000", "490154203237519", "１"*15, "123", None):
            self.assertFalse(m.valid_imei(v))

    def test_patch_only_named_values_preserves_size_and_other_digits(self):
        original = image()
        result, offsets = m.patch_devinfo(original, IMEIS)
        self.assertEqual(len(result), len(original))
        allowed = {i for offset in offsets.values() for i in range(offset, offset+15)}
        self.assertTrue(all(a == b or i in allowed for i, (a,b) in enumerate(zip(original, result))))
        self.assertIn(b"111111111111111\0", result)
        for name, offset in offsets.items():
            self.assertEqual(result[offset:offset+16], IMEIS[name].encode() + b"\0")

    def test_duplicate_record_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            m.imei_offsets(image() + record("imei1", b"000000000000000\0"))

    def test_truncated_record_rejected(self):
        with self.assertRaisesRegex(ValueError, "Malformed"):
            m.imei_offsets(b"DEVI"+bytes(124)+b"DIUS"+struct.pack("<II",1000,6)+b"imei1\0")

    def test_missing_and_unknown_layout_rejected(self):
        for data in (b"garbage", b"DEVI"+bytes(124)+b"490154203237518\0"):
            with self.assertRaises(ValueError):
                m.imei_offsets(data)

    def test_bad_size_and_terminator_rejected(self):
        for value in (b"123\0", b"0"*16):
            with self.assertRaisesRegex(ValueError, "field size"):
                m.imei_offsets(b"DEVI"+bytes(124)+record("imei1",value))

    def test_duplicate_imeis_rejected(self):
        with self.assertRaisesRegex(ValueError, "distinct"):
            m.patch_devinfo(image(), dict.fromkeys(IMEIS, IMEIS["imei1"]))

    def test_backup_tampering_rejected(self):
        backup = self.make_backup()
        m.check_backup(backup)
        (backup / "efs.img").write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "integrity"):
            m.check_backup(backup)

    def test_backup_without_completion_marker_rejected(self):
        with self.assertRaises(OSError):
            m.check_backup(self.root)

    def test_symlink_backup_rejected(self):
        backup = self.make_backup()
        (backup/"persist.img").unlink()
        (backup/"persist.img").symlink_to(backup/"efs.img")
        with self.assertRaisesRegex(ValueError, "integrity"):
            m.check_backup(backup)

    def test_plan_roundtrip_and_dry_run_never_connects(self):
        plan = self.make_plan()
        self.assertEqual(m.check_plan(plan)["device"], INFO)
        with patch.object(m, "Device", side_effect=AssertionError("Device touched")):
            m.apply(self.args(plan, apply=False))

    def test_non_imei_change_rejected_even_if_hash_updated(self):
        folder = self.make_plan()
        path = folder/"devinfo-restored.img"
        data = bytearray(path.read_bytes())
        data[-1] = 1
        path.write_bytes(data)
        plan = json.loads((folder/"plan.json").read_text())
        plan["restored_sha256"] = m.sha(path)
        (folder/"plan.json").write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "outside"):
            m.check_plan(folder)

    def test_explicit_write_flags_required_before_device_access(self):
        plan = self.make_plan()
        with patch.object(m, "Device", side_effect=AssertionError("Device touched")):
            for flag in ("confirm_original_imeis", "allow_unverified_build"):
                with self.assertRaisesRegex(ValueError, "requires"):
                    m.apply(self.args(plan, **{flag: False}))

    def test_wrong_serial_rejected_before_device_access(self):
        plan = self.make_plan()
        with patch.object(m, "Device", side_effect=AssertionError("Device touched")):
            with self.assertRaisesRegex(ValueError, "serial"):
                m.apply(self.args(plan, serial="OTHER"))

    def test_stale_live_partition_rejected_before_push(self):
        plan = self.make_plan()
        with patch.object(m, "Device") as cls:
            device = cls.return_value
            device.serial = "TEST123"
            device.info.return_value = INFO
            device.digest.return_value = "0"*64
            with self.assertRaisesRegex(ValueError, "stale"):
                m.apply(self.args(plan))
            device.call.assert_not_called()

    def test_changed_build_rejected_before_root(self):
        plan = self.make_plan()
        with patch.object(m, "Device") as cls:
            device = cls.return_value
            device.serial = "TEST123"
            device.info.return_value = dict(INFO, fingerprint="changed")
            with self.assertRaisesRegex(ValueError, "properties changed"):
                m.apply(self.args(plan))
            device.require_root.assert_not_called()

    def test_successful_write_targets_only_devinfo_and_checks_readback(self):
        folder = self.make_plan()
        plan = m.check_plan(folder)
        with patch.object(m, "Device") as cls:
            d = cls.return_value
            d.serial = "TEST123"
            d.info.return_value = INFO
            d.digest.side_effect = [plan["original_sha256"], plan["restored_sha256"]]
            d.size.return_value = len(image())
            d.call.return_value = b"/data/local/tmp/pixel-restore.ABCDEF\n"
            d.root.return_value = (plan["restored_sha256"] + "  staged\n").encode()
            m.apply(self.args(folder))
            commands = [c.args[0] for c in d.root.call_args_list]
            writes = [c for c in commands if c.startswith("dd ")]
            self.assertEqual(len(writes), 1)
            self.assertIn("of=/dev/block/by-name/devinfo", writes[0])
            self.assertNotIn("reboot", str(d.call.call_args_list))
            self.assertEqual(d.digest.call_count, 2)

    def test_failed_readback_surfaces_error(self):
        folder = self.make_plan()
        plan = m.check_plan(folder)
        with patch.object(m, "Device") as cls:
            d = cls.return_value
            d.serial = "TEST123"
            d.info.return_value = INFO
            d.digest.side_effect = [plan["original_sha256"], "0"*64]
            d.size.return_value = len(image())
            d.call.return_value = b"/data/local/tmp/pixel-restore.ABCDEF\n"
            d.root.return_value = (plan["restored_sha256"] + "  staged\n").encode()
            with self.assertRaisesRegex(ValueError, "WRITE VERIFICATION FAILED"):
                m.apply(self.args(folder))

    def test_backup_read_failure_leaves_no_completion_marker(self):
        with patch.object(m, "Device") as cls:
            d = cls.return_value
            d.serial = "TEST123"
            d.info.return_value = INFO
            d.size.return_value = 10
            d.digest.side_effect = ValueError("read failed")
            with self.assertRaisesRegex(ValueError, "read failed"):
                m.backup(argparse.Namespace(serial="TEST123", adb="adb", out=self.root/"bad"))
            self.assertFalse((self.root/"bad"/"manifest.json").exists())

    def test_log_summary_does_not_retain_identifiers(self):
        result = m.summarize_logs('Checkin failed: invalid hardware identifier: "FFFFFFFFFFFFFF4" is not a valid IMEI\nAUTHENTICATION_FAILED')
        self.assertTrue(result["invalid_imei_checkin"])
        self.assertNotIn("FFFFFFFF", json.dumps(result))

    def test_adb_always_selects_serial(self):
        with patch.object(m, "run", return_value=b"device\n") as run:
            d = m.Device("TEST123")
            d.call("shell", "getprop", "ro.product.device")
            for c in run.call_args_list:
                self.assertEqual(c.args[0][:3], ["adb", "-s", "TEST123"])

    def test_subprocess_failure_and_timeout(self):
        with patch.object(m.subprocess, "run") as proc:
            proc.return_value.returncode = 1
            proc.return_value.stderr = b"denied"
            with self.assertRaisesRegex(ValueError, "denied"):
                m.run(["adb"])
            proc.side_effect = m.subprocess.TimeoutExpired("adb", 30)
            with self.assertRaisesRegex(ValueError, "timed out"):
                m.run(["adb"])


    def test_unconfirmed_or_wrong_device_originals_rejected(self):
        backup = self.make_backup()
        original = self.root / "original.json"
        for record in (dict(IMEIS, serial="OTHER", confirmed_original=True),
                       dict(IMEIS, serial="TEST123", confirmed_original=False)):
            original.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                m.prepare(argparse.Namespace(backup=backup, original_imeis=original, out=self.root/"plan"))
            self.assertFalse((self.root/"plan").exists())

    def test_failed_backup_transfer_does_not_create_completion_marker(self):
        with patch.object(m, "Device") as cls:
            d = cls.return_value
            d.serial = "TEST123"
            d.info.return_value = INFO
            d.size.return_value = 100
            d.digest.return_value = "a"*64
            # root mock leaves the local output empty; length verification fails.
            with self.assertRaisesRegex(ValueError, "verification failed"):
                m.backup(argparse.Namespace(serial="TEST123", adb="adb", out=self.root/"bad"))
            self.assertFalse((self.root/"bad"/"manifest.json").exists())

    def test_complete_backup_verifies_every_partition(self):
        data = b"device block contents"
        digest = hashlib.sha256(data).hexdigest()
        with patch.object(m, "Device") as cls:
            d = cls.return_value
            d.serial = "TEST123"
            d.info.return_value = INFO
            d.size.return_value = len(data)
            d.digest.return_value = digest
            def read(command, **kwargs):
                if command.startswith("dd "):
                    kwargs["stdout"].write(data)
                    return b""
                return b"original cpsha"
            d.root.side_effect = read
            out = self.root/"good"
            m.backup(argparse.Namespace(serial="TEST123", adb="adb", out=out))
            self.assertEqual(set(m.check_backup(out)["files"]),
                             {p+".img" for p in m.PARTITIONS} | {"cpsha.bin"})
            self.assertEqual(d.digest.call_count, 8)

    def test_serial_optional_with_one_device(self):
        self.assertEqual(m.choose_serial(None, ["A"], "adb"), "A")
        self.assertEqual(m.choose_serial("B", ["A", "B"], "adb"), "B")
        with self.assertRaisesRegex(ValueError, "--serial"):
            m.choose_serial(None, ["A", "B"], "adb")
        with self.assertRaisesRegex(ValueError, "No adb"):
            m.choose_serial(None, [], "adb")

    def test_attached_parses_device_lists(self):
        adb = b"List of devices attached\nTEST123\tdevice\nOFF\tunauthorized\n\n"
        with patch.object(m, "run", return_value=adb):
            self.assertEqual(m.attached("adb", "device"), ["TEST123"])
        fb = b"TEST123\tfastboot\nOTHER\tfastbootd\n"
        with patch.object(m, "run", return_value=fb):
            self.assertEqual(m.attached("fastboot", "fastboot"), ["TEST123", "OTHER"])

    def test_adb_device_auto_selects_only_device(self):
        replies = {"devices": b"TEST123\tdevice\n", "get-state": b"device\n"}
        with patch.object(m, "run", side_effect=lambda argv, **k: replies[argv[-1]]) as run:
            d = m.Device(None)
            self.assertEqual(d.serial, "TEST123")
            self.assertEqual(run.call_args_list[-1].args[0][:3], ["adb", "-s", "TEST123"])

    def test_adb_apply_without_serial_checks_attached_device(self):
        plan = self.make_plan()
        with patch.object(m, "Device") as cls:
            cls.return_value.serial = "OTHER"
            with self.assertRaisesRegex(ValueError, "differs"):
                m.apply(self.args(plan, serial=None))
            cls.return_value.require_root.assert_not_called()

    def fastboot(self, serial="TEST123", **variables):
        values = {"product": "komodo", "unlocked": "yes"}
        values.update(variables)
        real = m.Fastboot
        phone = patch.object(m, "Fastboot").start()
        self.addCleanup(patch.stopall)
        phone.return_value.serial = serial
        phone.return_value.getvar.side_effect = values.get
        phone.return_value.require.side_effect = lambda product: real.require(phone.return_value, product)
        return phone.return_value

    def test_fastboot_devinfo_needs_no_readback_consent(self):
        plan = self.make_plan()
        with patch.object(m, "Fastboot", side_effect=AssertionError("Fastboot touched")):
            with self.assertRaisesRegex(ValueError, "--no-readback"):
                m.apply(self.args(plan, serial=None, fastboot=True))

    def test_fastboot_devinfo_flashes_only_devinfo(self):
        folder = self.make_plan()
        phone = self.fastboot(**{"partition-size:devinfo": hex(len(image()))})
        m.apply(self.args(folder, serial=None, fastboot=True, no_readback=True))
        phone.call.assert_called_once_with("flash", "devinfo", str(folder.resolve()/"devinfo-restored.img"))

    def test_fastboot_devinfo_rejects_wrong_phone_size_or_lock(self):
        folder = self.make_plan()
        for kwargs, message in ((dict(serial="OTHER"), "not the device"),
                                (dict(product="caiman"), "expected komodo"),
                                (dict(unlocked="no"), "locked"),
                                ({"partition-size:devinfo": "0x10"}, "bytes")):
            phone = self.fastboot(**kwargs)
            with self.assertRaisesRegex(ValueError, message):
                m.apply(self.args(folder, serial=None, fastboot=True, no_readback=True))
            phone.call.assert_not_called()
            patch.stopall()

    def radio_zip(self):
        path = self.root/"komodo-factory.zip"
        with m.zipfile.ZipFile(path, "w") as z:
            z.writestr("komodo-cp3a/radio-komodo-g5400c-250909-b-1.img", b"radio")
            z.writestr("komodo-cp3a/bootloader-komodo-x.img", b"bl")
        return path

    def test_reflash_radio_dry_run_flashes_nothing(self):
        phone = self.fastboot()
        m.reflash_radio(argparse.Namespace(image=self.radio_zip(), out=self.root/"out",
                                           apply=False, serial=None, fastboot_bin="fastboot"))
        phone.call.assert_not_called()
        self.assertEqual((self.root/"out"/"radio-komodo-g5400c-250909-b-1.img").read_bytes(), b"radio")

    def test_reflash_radio_flashes_and_checks_baseband(self):
        phone = self.fastboot(**{"version-baseband": "g5400c-250909-B-1"})
        m.reflash_radio(argparse.Namespace(image=self.radio_zip(), out=self.root/"out",
                                           apply=True, serial=None, fastboot_bin="fastboot"))
        self.assertEqual([c.args for c in phone.call.call_args_list],
                         [("flash", "radio", str(self.root/"out"/"radio-komodo-g5400c-250909-b-1.img")),
                          ("reboot-bootloader",)])

    def test_reflash_radio_rejects_other_model(self):
        self.fastboot(product="caiman")
        with self.assertRaisesRegex(ValueError, "expected komodo"):
            m.reflash_radio(argparse.Namespace(image=self.radio_zip(), out=self.root/"out",
                                               apply=True, serial=None, fastboot_bin="fastboot"))

    def test_fastboot_getvar_reads_stderr(self):
        with patch.object(m, "run", return_value=b"TEST123\tfastboot\n"):
            phone = m.Fastboot(None)
        with patch.object(m.subprocess, "run") as proc:
            proc.return_value.stderr = b"(bootloader) product: komodo\nOKAY\n"
            proc.return_value.stdout = b""
            self.assertEqual(phone.getvar("product"), "komodo")
            self.assertIsNone(phone.getvar("prod"))
            self.assertEqual(proc.call_args.args[0][:3], ["fastboot", "-s", "TEST123"])

    def diag_args(self, **kwargs):
        values = dict(serial=None, adb="adb", fastboot_bin="fastboot", fastboot=False, out=None)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_diagnose_mode_prefers_adb_and_falls_back_to_fastboot(self):
        lists = {"adb": [], "fastboot": ["FB1"]}
        with patch.object(m, "attached", side_effect=lambda tool, state: lists[tool]):
            self.assertEqual(m.diagnose_mode(self.diag_args()), "fastboot")
            self.assertEqual(m.diagnose_mode(self.diag_args(serial="FB1")), "fastboot")
            lists["adb"] = ["AD1"]
            self.assertEqual(m.diagnose_mode(self.diag_args()), "adb")
            self.assertEqual(m.diagnose_mode(self.diag_args(serial="FB1")), "fastboot")
            self.assertEqual(m.diagnose_mode(self.diag_args(fastboot=True)), "fastboot")

    def test_diagnose_mode_tolerates_missing_tool(self):
        def attached(tool, state):
            if tool == "fastboot":
                raise OSError("no fastboot")
            return []
        with patch.object(m, "attached", side_effect=attached):
            self.assertEqual(m.diagnose_mode(self.diag_args()), "adb")

    def test_diagnose_in_fastboot_reports_bootloader_state(self):
        phone = self.fastboot(**{"version-bootloader": "ripcurrentpro-17.0-1", "unlocked": "no"})
        phone.getvar_all.return_value = m.parse_getvar_all(GETVAR_ALL)
        phone.oem_config.return_value = m.parse_oem_config(OEM_CONFIG)
        phone.gsc_version.return_value = {"RO_A": "0.0.11/0d6b5dd7", "RW_A": "0.24.70/02e2916"}
        phone.gsc_headers.return_value = {"rw_a": {"version": "0.24.70"}}
        phone.gsc_avb_state.return_value = {"boot_lock": "1"}
        out = self.root / "report.json"
        m.diagnose(self.diag_args(fastboot=True, out=out))
        report = json.loads(out.read_text())
        self.assertEqual(report["mode"], "fastboot")
        self.assertEqual(report["bootloader"], "ripcurrentpro-17.0-1")
        self.assertEqual(report["baseband"], "g5400c-250909-250916-B-14110587")
        self.assertEqual(report["slot"], "a")
        self.assertEqual(report["modem"]["partitions"]["efs"], {"size": 0x1000000, "type": "raw"})
        self.assertEqual(report["modem"]["partitions"]["modem"]["slots"]["b"]["size"], 0x10000000)
        self.assertEqual(sorted(report["modem"]["partitions"]["modem"]["slots"]), ["a", "b"])
        self.assertEqual(report["slots"]["a"]["partitions"]["modem"]["size"], 0x10000000)
        self.assertNotIn("boot", report["modem"]["partitions"])
        self.assertEqual(report["modem"]["variables"], {})  # baseband is already a field
        f = report["findings"]
        self.assertFalse(f["baseband_missing"])
        self.assertTrue(f["bootloader_locked"])
        self.assertEqual(f["modem_partitions_missing"], ["devinfo"])
        self.assertEqual(f["empty_modem_partitions"], ["persist"])
        self.assertTrue(f["active_slot_modem_listed"])
        # Fields shown elsewhere are not repeated in the leftover variables.
        self.assertNotIn("hw-revision", report["variables"])
        self.assertNotIn("version-baseband", report["variables"])
        self.assertEqual(report["variables"], {
            "gsc": {"firmware_version": {"RO_A": "0.0.11/0d6b5dd7", "RW_A": "0.24.70/02e2916"},
                    "firmware_headers": {"rw_a": {"version": "0.24.70"}},
                    "avb_state": {"boot_lock": "1"}},
            "max-download-size": "0x10000000",
        })
        self.assertEqual(report["slots"]["b"]["partitions"]["boot"], {"size": 0x4000000})
        self.assertNotIn("boot", report["partitions"])
        self.assertNotIn("efs", report["partitions"])  # shown under modem instead
        self.assertEqual(report["variables"]["gsc"]["firmware_version"]["RW_A"], "0.24.70/02e2916")
        self.assertEqual({k: v for k, v in report["slots"]["a"].items() if k != "partitions"},
                         {"successful": True, "retry_count": "7"})
        self.assertEqual(report["oem_config"], {"fdr_allowed": "1"})  # modem keys live under modem
        self.assertEqual(report["modem"]["oem_config"], {"modem-boot": "ok", "sku": "GZC4K"})

    def test_parse_oem_config(self):
        self.assertEqual(m.parse_oem_config(OEM_CONFIG),
                         {"sku": "GZC4K", "modem-boot": "ok", "fdr_allowed": "1"})
        self.assertEqual(m.parse_oem_config("FAILED (remote: 'unknown command')\n"), {})

    def test_diagnose_without_oem_config_says_so(self):
        phone = self.fastboot()
        phone.getvar_all.return_value = {}
        phone.oem_config.return_value = {}
        phone.gsc_version.return_value = None
        phone.gsc_headers.return_value = {}
        phone.gsc_avb_state.return_value = None
        out = self.root / "report.json"
        m.diagnose(self.diag_args(fastboot=True, out=out))
        self.assertEqual(json.loads(out.read_text())["oem_config"], "not supported by this bootloader")

    def test_parse_getvar_all(self):
        got = m.parse_getvar_all(GETVAR_ALL + "(bootloader) product:komodo\nall: Done!\n")
        self.assertEqual(got["partition-size:efs"], "0x1000000")
        self.assertEqual(got["version-baseband"], "g5400c-250909-250916-B-14110587")
        self.assertEqual(got["product"], "komodo")
        self.assertNotIn("all", got)

    def backup_with(self, **images):
        out = self.root / "inspect"
        out.mkdir()
        good, _ = m.patch_devinfo(image(), IMEIS)
        files = {"devinfo.img": good, "efs.img": b"efs " + IMEIS["imei1"].encode(),
                 "efs_backup.img": b"efs " + IMEIS["imei1"].encode(),
                 "persist.img": b"persist CPSHA-BYTES persist", "cpsha.bin": b"CPSHA-BYTES"}
        files.update({k + ".img" if k != "cpsha" else "cpsha.bin": v for k, v in images.items()})
        manifest = {"schema": 1, "device": INFO.copy(), "files": {}}
        for name, data in files.items():
            (out / name).write_bytes(data)
            manifest["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        m.write_json(out / "manifest.json", manifest)
        return out

    def test_inspect_consistent_backup(self):
        oem = {"imei1": IMEIS["imei1"], "imei2": IMEIS["imei2"], "sku": "x"}
        r = m.inspect_backup(self.backup_with(), oem)
        self.assertTrue(r["consistent"], r["checks"])
        self.assertEqual(r["devinfo"]["imei1"], {"imei": "*" * 11 + "7518", "valid": True})
        self.assertEqual(r["devinfo_imeis_found_in"]["imei1"]["efs"], ["ascii"])
        self.assertNotIn(IMEIS["imei2"], json.dumps(r))

    def test_inspect_flags_disagreements(self):
        stray = next(c for c in ("35391805000000%d" % i for i in range(10)) if m.valid_imei(c))
        self.assertTrue(m.valid_imei(stray))
        r = m.inspect_backup(self.backup_with(efs_backup=b"older", persist=b"x " + stray.encode()),
                             {"imei1": stray}, show=True)
        c = r["checks"]
        self.assertFalse(r["consistent"])
        self.assertFalse(c["efs_matches_efs_backup"])
        self.assertFalse(c["cpsha_found_in_persist"])
        self.assertFalse(c["no_conflicting_plain_imeis"])
        self.assertEqual(r["other_imei_like_numbers"], {"persist": [stray]})
        self.assertFalse(c["oem_config_matches_devinfo"])

    def test_inspect_reports_broken_devinfo(self):
        r = m.inspect_backup(self.backup_with(devinfo=image()))
        self.assertFalse(r["checks"]["devinfo_imeis_valid"])
        (self.root / "inspect").rename(self.root / "inspect-old")
        r = m.inspect_backup(self.backup_with(devinfo=bytes(256)))
        self.assertIn("devinfo header", r["checks"]["devinfo_parsed"])
        self.assertFalse(r["consistent"])

    def test_imei_encodings(self):
        enc = m.imei_encodings("490154203237518")
        self.assertEqual(enc["tbcd"], bytes.fromhex("94104502237315f8"))
        self.assertEqual(enc["3gpp"], bytes.fromhex("4a09512430325781"))

    def test_ext4_superblock(self):
        sb = bytearray(4096)
        sb[1024+0x38:1024+0x3A] = (0xEF53).to_bytes(2, "little")
        sb[1024+0x78:1024+0x7D] = b"efs\0\0"
        sb[1024+0x3A] = 1
        info = m.image_summary(bytes(sb))
        self.assertEqual(info["format"], "ext4")
        self.assertEqual(info["ext4"]["label"], "efs")
        self.assertTrue(info["ext4"]["clean"])
        self.assertEqual(m.image_summary(b"\xff" * 64)["blank"], "every byte is 0xff")

    def test_diagnose_fastboot_rejects_pull(self):
        with patch.object(m, "Fastboot", side_effect=AssertionError("Fastboot touched")):
            with self.assertRaisesRegex(ValueError, "--pull needs Android"):
                m.diagnose(self.diag_args(fastboot=True, pull=str(self.root / "p")))

    def fbpk(self, *names):
        head = bytearray(112)
        head[:4] = b"FBPK"
        head[4:8] = (2).to_bytes(4, "little")
        head[104:108] = (len(names) + 1).to_bytes(4, "little")
        table = bytearray(104)  # partition table entry, kind 0
        for n in names:
            e = bytearray(104)
            e[:4] = (1).to_bytes(4, "little")
            e[4:4+len(n)] = n.encode()
            table += e
        return bytes(head + table)

    def factory_zip(self):
        inner = io.BytesIO()
        with m.zipfile.ZipFile(inner, "w") as z:
            for n in ("boot", "vendor_boot", "dtbo", "vbmeta", "super_empty", "system", "vendor"):
                z.writestr(n + ".img", b"x")
        path = self.root / "komodo-factory.zip"
        with m.zipfile.ZipFile(path, "w") as z:
            z.writestr("komodo-x/bootloader-komodo-ripcurrentpro-1.img", self.fbpk("bl1", "abl", "tzsw"))
            z.writestr("komodo-x/radio-komodo-g5400c-1.img", self.fbpk("modem"))
            z.writestr("komodo-x/image-komodo-x.zip", inner.getvalue())
        return path

    def test_package_partitions_and_coverage(self):
        pkg = m.package_partitions(self.factory_zip())
        self.assertEqual(pkg["abl"], "bootloader-komodo-ripcurrentpro-1.img")
        self.assertEqual(pkg["modem"], "radio-komodo-g5400c-1.img")
        self.assertEqual(pkg["boot"], "image-komodo-x.zip")
        phone = dict.fromkeys(["abl", "bl1", "tzsw", "modem", "boot", "dtbo", "efs", "persist",
                               "devinfo", "userdata", "gsa", "super"], {})
        cov = m.package_coverage(phone, pkg)
        self.assertEqual(sorted(cov["included"]), ["abl", "bl1", "boot", "dtbo", "modem", "super", "tzsw"])
        self.assertEqual(cov["not_included"]["device_unique"], ["devinfo", "efs", "persist", "userdata"])
        self.assertEqual(cov["not_included"]["other"], ["gsa"])
        self.assertIn("system", cov["package_only"])

    def test_tree_view_uses_friendly_names_and_flags_problems(self):
        report = {"serial": "TEST123", "product": "komodo",
                  "findings": {"baseband_missing": True, "supported_model": True},
                  "partitions": {"devinfo": {"size": 0x200000},
                                 "efs": {"slots": {"a": {"size": 0x1000000}}}}}
        plain = m.render_tree(report, color=False)
        self.assertIn("Manufacturer Device Info (devinfo)", plain)
        self.assertIn("Encrypted File Storage (efs)", plain)
        self.assertIn("Manufacturer Device Info (devinfo): 2 MiB (0x200000)", plain)
        self.assertIn("└─ ", plain)
        colored = m.render_tree(report)
        self.assertIn("\033[1;31myes\033[0m", colored)   # baseband missing: bad
        self.assertIn("\033[32myes\033[0m", colored)     # supported model: good

    def test_prefixed_variables_attach_to_partitions_or_group(self):
        parsed = m.structure_getvar(m.parse_getvar_all(
            "(bootloader) partition-size:abl_a: 0x10\n(bootloader) partition-size:abl_b: 0x10\n"
            "(bootloader) partition-size:tzsw: 0x10\n(bootloader) sbdp:abl: no\n"
            "(bootloader) sbdp:tzsw: yes\n(bootloader) sbdp:ar-check: no\n(bootloader) sbdp:allow: no\n"))
        self.assertIs(parsed["partitions"]["abl"]["sbdp"], False)
        self.assertIs(parsed["partitions"]["tzsw"]["sbdp"], True)
        self.assertEqual(parsed["variables"], {"sbdp": {"allow": False, "ar-check": False}})
        self.assertIn("Anti-Rollback Check (ar-check): no", m.render_tree(parsed, color=False))

    def test_all_slot_variables_join_slot_objects(self):
        parsed = m.structure_getvar(m.parse_getvar_all(
            "(bootloader) slot-fastboot-ok:a: no\n(bootloader) slot-fastboot-ok:b: yes\n"
            "(bootloader) slot-suffixes: _a,_b\n(bootloader) slot-count: 2\n"
            "(bootloader) slot-retry-count:b: 7\n"))
        self.assertEqual(parsed["slots"], {"a": {"fastboot_ok": False},
                                           "b": {"fastboot_ok": True, "retry_count": "7"}})
        self.assertEqual(parsed["variables"], {})
        tree = m.render_tree({"slot": "b", "slots": parsed["slots"],
                              "partitions": {"abl": {"slots": {"a": {"size": 16}, "b": {"size": 16}}}}},
                             color=False)
        self.assertIn("├─ Slot a [no fastboot]\n│  └─ Slot b", tree)
        self.assertIn("└─ Slot b [active, fastboot ok, 7 retries left]\n", tree)
        self.assertIn("Slot b [active, fastboot ok, 7 retries left]: 16 B", tree)

    def test_security_component_variables(self):
        text = ("(bootloader) gsa-bl1-ar: 2\n(bootloader) gsa-fw-ar: 2\n(bootloader) gsc-flavor: d3m2\n"
                "(bootloader) gsc-ro: 9b4e3277\n(bootloader) gsc-rw: 02e2916\n")
        parsed = m.structure_getvar(m.parse_getvar_all(text))
        self.assertEqual(parsed["variables"], {
            "gsa": {"bl1_anti_rollback": 2, "fw_anti_rollback": 2},
            "gsc": {"flavor": "d3m2", "ro": "9b4e3277", "rw": "02e2916", "chip": "Titan M2 (Dauntless)"}})
        # With the partitions present, the levels attach to them instead.
        parsed = m.structure_getvar(m.parse_getvar_all(
            text + "(bootloader) partition-size:gsa_bl1: 0x10\n(bootloader) partition-size:gsa: 0x10\n"))
        self.assertEqual(parsed["partitions"]["gsa_bl1"]["anti_rollback"], 2)
        self.assertEqual(parsed["partitions"]["gsa"]["anti_rollback"], 2)
        self.assertEqual(parsed["variables"]["gsc"]["chip"], "Titan M2 (Dauntless)")
        self.assertEqual(list(parsed["variables"]), ["gsc"])
        self.assertEqual(m.titan_model("c1"), "Titan M (Citadel)")
        self.assertIn("unknown", m.titan_model("x9"))
        tree = m.render_tree(parsed, color=False)
        self.assertIn("Titan M (gsc)", tree)
        self.assertIn("Read-Only Firmware (ro): 9b4e3277", tree)
        parsed = m.structure_getvar(m.parse_getvar_all("(bootloader) ap-ar-ns: 2\n(bootloader) ap-ar-s: 3\n"))
        self.assertEqual(parsed["variables"], {"ap": {"anti_rollback_ns": 2, "anti_rollback_s": 3}})
        self.assertIn("Application Processor (ap)", m.render_tree(parsed, color=False))

    def test_regroup_moves_slotted_partitions_and_vbmeta(self):
        parsed = m.structure_getvar(m.parse_getvar_all(
            "(bootloader) partition-size:boot_a: 0x10\n(bootloader) partition-size:boot_b: 0x10\n"
            "(bootloader) partition-size:vbmeta_a: 0x1\n(bootloader) partition-size:vbmeta_b: 0x1\n"
            "(bootloader) partition-size:vendor_boot_a: 0x20\n(bootloader) partition-size:vendor_boot_b: 0x20\n"
            "(bootloader) partition-size:vbmeta_vendor_boot_a: 0x2\n"
            "(bootloader) partition-size:vbmeta_vendor_boot_b: 0x2\n"
            "(bootloader) partition-size:efs: 0x30\n(bootloader) partition-size:vbmeta_efs: 0x3\n"
            "(bootloader) slot-successful:a: yes\n(bootloader) sbdp:boot: no\n"))
        unslotted, slots = m.regroup(parsed["partitions"], parsed["slots"])
        self.assertEqual(unslotted, {"efs": {"size": 0x30, "vbmeta": {"size": 3}}})
        self.assertEqual(slots["a"]["successful"], True)
        self.assertEqual(slots["a"]["partitions"]["vendor_boot"], {"size": 0x20, "vbmeta": {"size": 2}})
        self.assertEqual(slots["b"]["partitions"]["boot"], {"size": 0x10, "sbdp": False})
        self.assertIn("vbmeta", slots["b"]["partitions"])  # plain vbmeta covers the boot chain
        tree = m.render_tree({"slot": "a", "slots": slots, "partitions": unslotted}, color=False)
        self.assertIn("├─ Slot a [active, booted]\n│  │  ├─ Kernel Boot Image (boot)", tree)
        self.assertIn("└─ Vendor Boot Image (vendor_boot)\n│  │     ├─ Size (size): 32 B (0x20)\n"
                      "│  │     └─ Verified Boot Metadata (vbmeta): 2 B (0x2)", tree)

    def test_gsc_version_parses_fields_or_keeps_lines(self):
        with patch.object(m, "run", return_value=b"TEST123\tfastboot\n"):
            phone = m.Fastboot(None)
        with patch.object(phone, "oem", return_value="(bootloader) RW_A: 0.24.70\nOKAY\n"):
            self.assertEqual(phone.gsc_version(), {"RW_A": "0.24.70"})
        with patch.object(phone, "oem", return_value="(bootloader) Titan M2 d3m2 rw 0.24\nOKAY\n"):
            self.assertEqual(phone.gsc_version(), ["Titan M2 d3m2 rw 0.24"])
        with patch.object(phone, "oem", return_value="FAILED (remote: 'unknown command')\n"):
            self.assertIsNone(phone.gsc_version())
        with patch.object(phone, "oem", return_value="(bootloader) boot_lock: 1\nOKAY\n") as oem:
            self.assertEqual(phone.gsc_avb_state(), {"boot_lock": "1"})
        oem.assert_called_once_with("gsc", "avb", "state")

    def test_gsc_version_lines_split_into_fields(self):
        self.assertEqual(m.parse_gsc_image("* 0.2.114/9b4e3277 ok"),
                         {"running": True, "image_version": "0.2.114", "build": "9b4e3277",
                          "state": "ok", "commit": "9b4e3277"})
        self.assertEqual(m.parse_gsc_image("0.2.2/nugget_v0.0.ab15116366-6c96681-r ok"),
                         {"running": False, "image_version": "0.2.2",
                          "build": "nugget_v0.0.ab15116366-6c96681-r", "state": "ok",
                          "build_id": "15116366", "commit": "6c96681"})
        self.assertEqual(m.parse_gsc_image("odd"), "odd")
        with patch.object(m, "run", return_value=b"TEST123\tfastboot\n"):
            phone = m.Fastboot(None)
        text = ("(bootloader) RO_A: * 0.2.114/9b4e3277 ok\n(bootloader) RO_B: 0.2.112/5cfcbae6 ok\n"
                "(bootloader) RW_A: * 0.2.2/nugget_v0.0.ab15203921-02e2916-r ok\n"
                "(bootloader) RW_B: 0.2.2/nugget_v0.0.ab15116366-6c96681-r ok\nOKAY\n")
        with patch.object(phone, "oem", return_value=text):
            version = phone.gsc_version()
        self.assertTrue(version["RW_A"]["running"])
        self.assertEqual(version["RW_A"]["commit"], "02e2916")
        gsc = {"ro": "9b4e3277", "rw": "02e2916", "firmware_version": version}
        self.assertEqual(m.gsc_running_matches(gsc), {"ro": True, "rw": True})
        self.assertEqual(m.gsc_running_matches(dict(gsc, rw="6c96681")), {"ro": True, "rw": False})
        self.assertIsNone(m.gsc_running_matches({"ro": "x"}))

    def test_gsc_header_decodes_signed_header(self):
        text = (            '(bootloader) hdr.magic: fffffffd (Dauntless)\n'
            '(bootloader) hdr.ro_base: 00120000\n'
            '(bootloader) hdr.keyid: 68f379bd\n'
            '(bootloader) hdr.tag: 30333030303030303034303030303030303030303030303030303030\n'
            '(bootloader) hdr.epoch: 00000000\n'
            '(bootloader) hdr.major: 00000002\n'
            '(bootloader) hdr.minor: 00000002\n'
            '(bootloader) hdr.timestamp: 0000000069cb788c\n'
            '(bootloader) hdr.img_size: 00040000\n'
            '(bootloader) hdr.img_chk: f4ae83cc\n'
            '(bootloader) hdr.fuses_chk: f971f66f\n'
            '(bootloader) hdr.info_chk: e82ff4b8\n'
            '(bootloader) hdr.applysec: ffffffff\n'
            '(bootloader) hdr.config1: 00002706\n'
            '(bootloader) hdr.err_response: 0000000c\n'
            '(bootloader) hdr.expect_response: 00000003\n'
            '(bootloader) hdr.fusemap: 00000100402000000000040000040000\n'
            '(bootloader) hdr.infomap: FFFFFFFCFFFFFFFFFFFFFFFFFFFFFFFF\n'
            '(bootloader) hdr.board_id: 00000000 00000000 00000000\n'
            'OKAY\n')
        h = m.parse_gsc_header(m.parse_oem_config(text))
        self.assertEqual(h["magic"], {"value": "fffffffd", "chip": "Dauntless (Titan M2)"})
        self.assertEqual(h["keyid"]["signed_for"], "production (Cr50 rule, unconfirmed for this chip)")
        h1 = m.parse_gsc_header({"hdr.magic": "ffffffff", "hdr.keyid": "894b7d29"})
        self.assertEqual(h1["keyid"]["signed_for"], "development")
        self.assertEqual(h["tag"]["text"], "0300000004000000000000000000")
        self.assertEqual(h["timestamp"]["utc"], "2026-03-31T07:32:28+00:00")
        self.assertEqual(h["image_version"], "0.2.2")
        self.assertEqual(h["ro_base"], "0x00120000")
        self.assertEqual(h["fusemap"]["fuses_checked"], [8, 53, 62, 74, 114])
        self.assertEqual(h["infomap"]["rollback_level"], 2)
        self.assertTrue(h["board_id"]["any_board"])
        self.assertEqual(h["config1"], "00002706")
        gsc = {"firmware_headers": {"rw_b": h},
               "firmware_version": {"RW_B": m.parse_gsc_image("0.2.2/nugget_v0.0.ab15116366-6c96681-r ok")}}
        self.assertEqual(m.gsc_headers_match_versions(gsc), {"rw_b": True})

    def test_gsc_avb_state_names_fields(self):
        text = ("(bootloader) GSC AVB state:\n(bootloader) Version: : 1\n"
                "(bootloader) Bootloader state: 1\n(bootloader) Production state: 1\n"
                "(bootloader) Number of locks: 4\n(bootloader) Lock[0]: 0\n(bootloader) Lock[1]: 1\n"
                "(bootloader) Lock[2]: 0\n(bootloader) Lock[3]: 1\nOKAY\n")
        state = m.parse_gsc_avb_state(m.parse_oem_config(text))
        self.assertEqual(state, {
            "avb_app_version": 1, "in_bootloader": True, "production": True, "lock_count": 4,
            "locks": {"carrier_lock": {"locked": False, "index": 0}, "device_lock": {"locked": True, "index": 1},
                      "boot_lock": {"locked": False, "index": 2}, "owner_lock": {"locked": True, "index": 3}}})

    def test_gsc_headers_reads_each_image(self):
        with patch.object(m, "run", return_value=b"TEST123\tfastboot\n"):
            phone = m.Fastboot(None)
        replies = {"ro_a": "(bootloader) version: 0.0.11\nOKAY\n",
                   "rw_b": "(bootloader) rw_b header magic 0x1\nOKAY\n"}
        calls = []

        def oem(*args):
            calls.append(args)
            return replies.get(args[-1], "FAILED (remote: 'invalid image')\n")
        with patch.object(phone, "oem", side_effect=oem):
            self.assertEqual(phone.gsc_headers(), {"ro_a": {"version": "0.0.11"},
                                                   "rw_b": ["rw_b header magic 0x1"]})
        self.assertEqual(calls, [("gsc", "header", i) for i in ("ro_a", "ro_b", "rw_a", "rw_b")])

    def test_identify_recognizes_partition_formats(self):
        vbmeta = bytearray(256)
        vbmeta[:4] = b"AVB0"
        vbmeta[4:8] = (1).to_bytes(4, "big")
        vbmeta[112:120] = (5).to_bytes(8, "big")
        vbmeta[128:136] = b"avbtool\0"
        kind, info = m.identify(bytes(vbmeta))
        self.assertEqual((kind, info["rollback_index"], info["release"], info["libavb"]),
                         ("vbmeta", 5, "avbtool", "1.0"))
        boot = bytearray(8192)
        boot[:8] = b"ANDROID!"
        boot[40:44] = (4).to_bytes(4, "little")
        boot[4096:4100] = b"AVB0"
        boot[-64:-60] = b"AVBf"
        boot[-52:-44] = (4096).to_bytes(8, "big")
        boot[-44:-36] = (4096).to_bytes(8, "big")
        kind, info = m.identify(bytes(boot))
        self.assertEqual((kind, info["header_version"]), ("android_boot_image", 4))
        self.assertEqual(info["avb_footer"]["vbmeta_offset"], 4096)
        self.assertEqual(m.identify(b"\xff" * 64), ("blank", {"byte": "0xff"}))
        self.assertEqual(m.identify(image())[0], "devinfo")
        stage = bytearray(2048)
        stage[0x400:0x404] = b"APBL"
        self.assertEqual(m.identify(bytes(stage)), ("tensor_boot_stage", {"tag": "APBL"}))
        ext4 = bytearray(4096)
        ext4[1024 + 0x38:1024 + 0x3A] = (0xEF53).to_bytes(2, "little")
        self.assertEqual(m.identify(bytes(ext4))[0], "ext4")
        self.assertEqual(m.identify(b"\x12\x34" + b"\0" * 30)[0], "unknown")
        self.assertEqual(m.parse_size("100M"), 100 << 20)
        self.assertEqual(m.parse_size("64k"), 64 << 10)

    def test_fetch_passes_slot_without_suffix(self):
        with patch.object(m, "run", return_value=b"TEST123\tfastboot\n"):
            phone = m.Fastboot(None)
        out = self.root / "boot.img"
        done = argparse.Namespace(returncode=0, stdout=b"", stderr=b"OKAY\n")

        def fake(argv, **kwargs):
            out.write_bytes(b"x")
            return done
        with patch.object(m.subprocess, "run", side_effect=fake) as call:
            self.assertIsNone(phone.fetch("boot", out, "a"))
        self.assertEqual(call.call_args[0][0],
                         ["fastboot", "-s", "TEST123", "--slot", "a", "fetch", "boot", str(out)])

    def test_read_partitions_from_fastboot_fetch(self):
        phone = self.fastboot()
        phone.getvar_all.return_value = m.parse_getvar_all(GETVAR_ALL)
        fetched = []

        def fetch(name, path, slot=None):
            fetched.append((name, slot))
            if name == "efs":
                return "FAILED (remote: 'Fetch is not allowed on this partition')"
            Path(path).write_bytes(b"\0" * 16 if name == "persist" else b"ANDROID!" + b"\1" * 56)
            return None
        phone.fetch.side_effect = fetch
        out = self.root / "read"
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            m.read_partitions(argparse.Namespace(
                out=str(out), max_size=0x8000000, fastboot=True, serial=None, adb="adb",
                fastboot_bin="fastboot", json=True, show_identifiers=False))
        report = json.loads((out / "report.json").read_text())
        self.assertNotIn(("modem", "a"), fetched)  # 256 MiB, over the limit
        self.assertIn(("boot", "a"), fetched)  # suffix dropped, slot passed with --slot
        self.assertIn(("efs", None), fetched)
        self.assertIn("modem_a", report["skipped_over_limit"])
        self.assertEqual(report["unreadable"]["efs"], "FAILED (remote: 'Fetch is not allowed on this partition')")
        self.assertEqual(report["partitions"]["persist"]["format"], "blank")
        self.assertIn("boot_a", report["summary"]["parsed"])
        self.assertEqual(report["partitions"]["boot_a"]["format"], "android_boot_image")
        self.assertEqual(report["summary"]["unreadable_reasons"],
                         {"FAILED (remote: 'Fetch is not allowed on this partition')": 1})

    def test_read_partitions_hints_at_fastbootd(self):
        phone = self.fastboot(unlocked="no")
        phone.getvar_all.return_value = m.parse_getvar_all(GETVAR_ALL)
        phone.fetch.return_value = "fastboot: error: Command failed"
        out = self.root / "read"
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            m.read_partitions(argparse.Namespace(
                out=str(out), max_size=0x8000000, fastboot=True, serial=None, adb="adb",
                fastboot_bin="fastboot", json=True, show_identifiers=False))
        report = json.loads((out / "report.json").read_text())
        self.assertIsNone(report["userspace"])
        self.assertIn("fastboot reboot fastboot", report["hint"])


if __name__ == "__main__":
    unittest.main()
