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
                      allow_unverified_build=True, serial="TEST123", adb="adb")
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
            device.info.return_value = INFO
            device.digest.return_value = "0"*64
            with self.assertRaisesRegex(ValueError, "stale"):
                m.apply(self.args(plan))
            device.call.assert_not_called()

    def test_changed_build_rejected_before_root(self):
        plan = self.make_plan()
        with patch.object(m, "Device") as cls:
            device = cls.return_value
            device.info.return_value = dict(INFO, fingerprint="changed")
            with self.assertRaisesRegex(ValueError, "properties changed"):
                m.apply(self.args(plan))
            device.require_root.assert_not_called()

    def test_successful_write_targets_only_devinfo_and_checks_readback(self):
        folder = self.make_plan()
        plan = m.check_plan(folder)
        with patch.object(m, "Device") as cls:
            d = cls.return_value
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


if __name__ == "__main__":
    unittest.main()
