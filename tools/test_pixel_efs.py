"""Offline tests for pixel_efs.py: synthetic images, a fake fastboot, no phone."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("pixel_efs", Path(__file__).with_name("pixel_efs.py"))
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)

MIB = 1 << 20
HAVE_E2FS = shutil.which("mke2fs") and shutil.which("debugfs")


def ext4_image(size=4 * MIB, files=None, label="efs"):
    """A real ext4 image built with mke2fs/debugfs (skips the test without them)."""
    if not HAVE_E2FS:
        raise unittest.SkipTest("mke2fs/debugfs not installed")
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "fs.img"
        img.write_bytes(bytes(size))
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-b", "4096", "-L", label, "-F", str(img)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name, content in (files or {}).items():
            src = Path(tmp) / name
            src.write_bytes(content)
            subprocess.run(["debugfs", "-w", "-R", f"write {src} {name}", str(img)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return img.read_bytes()


def f2fs_image(size=4 * MIB, blocks=None, label="efs", second=True, corrupt_second=False):
    """Just the two f2fs superblocks, enough for the classifier."""
    data = bytearray(size)
    sb = bytearray(3072)
    struct.pack_into("<IHH", sb, 0, e.F2FS_MAGIC, 1, 16)
    struct.pack_into("<I", sb, 0x10, 12)                      # log_blocksize: 4096
    struct.pack_into("<Q", sb, 0x24, blocks if blocks is not None else size // 4096)
    sb[0x6C:0x7C] = bytes(range(16))
    sb[0x7C:0x7C + 2 * len(label)] = label.encode("utf-16-le")
    data[1024:1024 + 3072] = sb
    if second:
        if corrupt_second:
            struct.pack_into("<Q", sb, 0x24, 1)
        data[4096 + 1024:4096 + 1024 + 3072] = sb
    data[8192:8192 + 17] = b"not a blank image"
    return bytes(data)


class Classify(unittest.TestCase):
    def test_blank_images(self):
        zero = e.classify(bytes(MIB))
        self.assertEqual((zero["kind"], zero["health"], zero["wiped_for_fs_mgr"]), ("zero", "blank", True))
        erased = e.classify(b"\xff" * MIB)
        self.assertEqual((erased["kind"], erased["health"], erased["wiped_for_fs_mgr"]), ("erased", "blank", False))
        self.assertEqual(e.classify(b"")["health"], "blank")

    def test_unknown_contents(self):
        entry = e.classify(os.urandom(MIB))
        self.assertEqual((entry["kind"], entry["health"]), ("unknown", "damaged"))
        self.assertIn("no ext4 or f2fs superblock", entry["reason"])

    def test_ext4_with_nv_files(self):
        data = ext4_image(files={"nv_normal.bin": b"nv" * 100, "nv_normal.bin.md5": b"x" * 32, "other": b"o"})
        entry = e.classify(data)
        self.assertEqual((entry["kind"], entry["health"]), ("ext4", "healthy"))
        self.assertEqual(entry["ext4"]["label"], "efs")
        self.assertEqual(entry["ext4"]["fs_bytes"], 4 * MIB)
        self.assertEqual(entry["nv_files"], ["nv_normal.bin", "nv_normal.bin.md5"])
        self.assertEqual({f["path"]: f["size"] for f in entry["files"]},
                         {"/nv_normal.bin": 200, "/nv_normal.bin.md5": 32, "/other": 1})

    def test_ext4_empty_and_truncated(self):
        self.assertEqual(e.classify(ext4_image())["health"], "empty")
        truncated = ext4_image()[:2 * MIB]
        entry = e.classify(truncated)
        self.assertEqual(entry["health"], "damaged")
        self.assertIn("claims", entry["reason"])

    def test_f2fs(self):
        entry = e.classify(f2fs_image())
        self.assertEqual((entry["kind"], entry["health"]), ("f2fs", "healthy"))
        self.assertEqual(entry["f2fs"]["label"], "efs")
        self.assertEqual(entry["f2fs"]["fs_bytes"], 4 * MIB)
        self.assertEqual(entry["f2fs"]["superblocks"], 2)
        self.assertEqual(e.classify(f2fs_image(blocks=4096 * 4))["health"], "damaged")
        self.assertEqual(e.classify(f2fs_image(corrupt_second=True))["reason"], "the two f2fs superblocks differ")
        only_backup = bytearray(f2fs_image())
        only_backup[1024:1024 + 8] = bytes(8)        # primary superblock gone
        self.assertEqual(e.classify(bytes(only_backup))["f2fs"]["superblocks"], 1)


class Inspect(unittest.TestCase):
    def test_cross_checks(self):
        good = ext4_image(files={"nv_normal.bin": b"a", "nv_protected.bin": b"b"})
        other = ext4_image(files={"nv_normal.bin": b"aa", "nv_normal.bin.md5": b"m"})
        report = e.inspect_images({"efs": good, "efs_backup": other})
        checks = report["checks"]
        self.assertTrue(checks["efs_backup_same_size"])
        self.assertFalse(checks["efs_matches_efs_backup"])
        self.assertEqual(checks["only_in_efs"], ["/nv_protected.bin"])
        self.assertEqual(checks["only_in_efs_backup"], ["/nv_normal.bin.md5"])
        self.assertEqual(checks["same_name_different_size"], ["/nv_normal.bin"])
        self.assertEqual(checks["efs_nv_files_missing"], ["nv_normal.bin.md5"])
        self.assertEqual(report["healthy"], ["efs", "efs_backup"])
        self.assertEqual(report["unhealthy"], [])

    def test_blank_efs(self):
        report = e.inspect_images({"efs": bytes(4 * MIB), "efs_backup": f2fs_image()})
        self.assertEqual(report["unhealthy"], ["efs"])
        self.assertTrue(report["checks"]["efs_backup_same_size"])


class Decide(unittest.TestCase):
    def current(self, **images):
        return e.inspect_images(images)

    def test_mirror_restores_blank_efs(self):
        d = e.decide(self.current(efs=bytes(4 * MIB), efs_backup=f2fs_image()), {})
        self.assertEqual(d["efs"]["action"], "flash")
        self.assertEqual(d["efs"]["source"], "phone:efs_backup")
        self.assertEqual(d["efs"]["sha256"], e.sha(f2fs_image()))
        self.assertEqual(d["efs_backup"]["action"], "keep")
        self.assertEqual(d["modem_userdata"]["action"], "unresolved")
        self.assertEqual(d["persist"]["action"], "unresolved")

    def test_mirror_restores_blank_backup(self):
        d = e.decide(self.current(efs=f2fs_image(), efs_backup=b"\xff" * (4 * MIB)), {})
        self.assertEqual((d["efs"]["action"], d["efs_backup"]["source"]), ("keep", "phone:efs"))

    def test_mirror_size_must_match_partition(self):
        cur = self.current(efs=bytes(4 * MIB), efs_backup=f2fs_image(size=2 * MIB))
        d = e.decide(cur, {"efs": 4 * MIB})
        self.assertEqual(d["efs"]["action"], "unresolved")
        self.assertIn("not 4194304", d["efs"]["why"])

    def test_healthy_copies_that_differ_are_left_unless_preferred(self):
        a, b = f2fs_image(label="efs"), f2fs_image(label="efs2")
        cur = self.current(efs=a, efs_backup=b)
        self.assertEqual({d["action"] for d in e.decide(cur, {}).values() if d["action"] != "unresolved"}, {"keep"})
        d = e.decide(cur, {}, prefer="efs")
        self.assertEqual((d["efs"]["action"], d["efs_backup"]["source"]), ("keep", "phone:efs"))
        d = e.decide(cur, {}, prefer="efs_backup")
        self.assertEqual((d["efs"]["source"], d["efs_backup"]["action"]), ("phone:efs_backup", "keep"))

    def test_backup_is_used_when_both_copies_are_gone(self):
        cur = self.current(efs=bytes(4 * MIB), efs_backup=bytes(4 * MIB), persist=os.urandom(MIB))
        backup = {"efs": e.classify(f2fs_image()), "efs_backup": e.classify(f2fs_image()),
                  "persist": e.classify(f2fs_image(size=MIB))}
        d = e.decide(cur, {}, backup)
        self.assertEqual({n: d[n]["source"] for n in ("efs", "efs_backup", "persist")},
                         {"efs": "backup:efs", "efs_backup": "backup:efs_backup", "persist": "backup:persist"})
        # A backup copy of the wrong size is refused, like a mirror of the wrong size.
        wrong = dict(backup, persist=e.classify(f2fs_image(size=2 * MIB)))
        self.assertEqual(e.decide(cur, {}, wrong)["persist"]["action"], "unresolved")

    def test_allow_empty_only_for_formattable_partitions(self):
        cur = self.current(efs=bytes(4 * MIB), efs_backup=bytes(4 * MIB), modem_userdata=bytes(MIB),
                           persist=os.urandom(MIB))
        d = e.decide(cur, {}, allow_empty=True)
        for name in ("efs", "efs_backup", "modem_userdata"):
            self.assertEqual((d[name]["action"], d[name]["source"]), ("flash", "empty"), name)
            self.assertEqual(d[name]["sha256"], e.sha(bytes(cur["partitions"][name]["size"])))
        self.assertEqual(d["persist"]["action"], "unresolved")
        self.assertIn("never emptied", d["persist"]["why"])
        # Without the flag nothing is emptied.
        self.assertTrue(all(v["action"] == "unresolved" for v in e.decide(cur, {}).values()))

    def test_empty_filesystem_is_kept_when_nothing_can_refill_it(self):
        d = e.decide(self.current(modem_userdata=ext4_image()), {})
        self.assertEqual(d["modem_userdata"]["action"], "keep")
        self.assertIn("empty filesystem mounts", d["modem_userdata"]["why"])


class PlanAndApply(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.images = self.tmp / "images"
        self.images.mkdir()
        self.good = f2fs_image()
        (self.images / "efs.img").write_bytes(bytes(4 * MIB))
        (self.images / "efs_backup.img").write_bytes(self.good)
        (self.images / "report.json").write_text(json.dumps({
            "device": {"serial": "SER1", "product": "komodo", "mode": "fastboot"},
            "sizes": {"efs": 4 * MIB, "efs_backup": 4 * MIB}}))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make_plan(self, *extra):
        out = self.tmp / "plan"
        self.assertEqual(e.main(["plan", "--images", str(self.images), "--out", str(out), *extra]), 0)
        return out

    def test_plan_copies_the_source_image(self):
        out = self.make_plan()
        spec = json.loads((out / "plan.json").read_text())
        self.assertEqual(spec["device"], {"serial": "SER1", "product": "komodo"})
        self.assertEqual([a["partition"] for a in spec["actions"]], ["efs"])
        self.assertEqual((out / "efs.img").read_bytes(), self.good)
        self.assertEqual(spec["actions"][0]["sha256"], e.sha(self.good))
        self.assertEqual(spec["unresolved"], ["modem_userdata", "persist"])
        self.assertEqual(e.check_plan(out)["schema"], 1)

    def test_plan_refuses_another_phones_backup(self):
        backup = self.tmp / "backup"
        backup.mkdir()
        (backup / "efs.img").write_bytes(self.good)
        (backup / "manifest.json").write_text(json.dumps({
            "schema": 1, "device": {"serial": "OTHER"},
            "files": {"efs.img": {"size": len(self.good), "sha256": e.sha(self.good)}}}))
        self.assertEqual(e.main(["plan", "--images", str(self.images), "--out", str(self.tmp / "p2"),
                                 "--backup", str(backup)]), 1)
        self.assertFalse((self.tmp / "p2").exists())
        # A backup without a serial needs an explicit attestation.
        (backup / "manifest.json").unlink()
        self.assertEqual(e.main(["plan", "--images", str(self.images), "--out", str(self.tmp / "p3"),
                                 "--backup", str(backup)]), 1)
        self.assertEqual(e.main(["plan", "--images", str(self.images), "--out", str(self.tmp / "p4"),
                                 "--backup", str(backup), "--backup-is-this-phone"]), 0)

    def test_plan_rejects_tampered_backup(self):
        backup = self.tmp / "backup"
        backup.mkdir()
        (backup / "efs.img").write_bytes(self.good)
        (backup / "manifest.json").write_text(json.dumps({
            "schema": 1, "device": {"serial": "SER1"},
            "files": {"efs.img": {"size": len(self.good), "sha256": "0" * 64}}}))
        self.assertEqual(e.main(["plan", "--images", str(self.images), "--out", str(self.tmp / "p"),
                                 "--backup", str(backup)]), 1)

    def test_plan_image_must_not_change(self):
        out = self.make_plan()
        (out / "efs.img").write_bytes(f2fs_image(label="x"))
        with self.assertRaises(ValueError):
            e.check_plan(out)

    def fake_fastboot(self, serial="SER1", product="komodo", unlocked="yes", userspace="no", size=4 * MIB):
        """A fastboot stand-in that answers getvar and logs flashes."""
        log = self.tmp / "flash.log"
        script = self.tmp / "fastboot"
        script.write_text(f"""#!/bin/sh
cmd=""; for a in "$@"; do last="$a"; case "$a" in -s) ;; {serial}) ;; *) cmd="$cmd $a";; esac; done
case "$cmd" in
  " devices") echo "{serial}	fastboot";;
  " getvar product") echo "product: {product}" >&2;;
  " getvar unlocked") echo "unlocked: {unlocked}" >&2;;
  " getvar is-userspace") echo "is-userspace: {userspace}" >&2;;
  " getvar partition-size:efs") echo "partition-size:efs: {size:#x}" >&2;;
  " getvar"*) echo "$cmd: ?" >&2;;
  " flash"*) echo "$cmd" >> {log};;
  *) echo "unexpected: $cmd" >&2; exit 1;;
esac
""")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script, log

    def test_apply_dry_run_touches_nothing(self):
        out = self.make_plan()
        script, log = self.fake_fastboot()
        self.assertEqual(e.main(["apply", "--plan", str(out), "--fastboot-bin", str(script)]), 0)
        self.assertFalse(log.exists())

    def test_apply_flashes_after_checks(self):
        out = self.make_plan()
        script, log = self.fake_fastboot()
        with patch("builtins.input", return_value="SER1"):
            self.assertEqual(e.main(["apply", "--plan", str(out), "--apply", "--fastboot-bin", str(script)]), 0)
        self.assertEqual(log.read_text().strip(), f"flash efs {out / 'efs.img'}")

    def test_apply_refusals(self):
        out = self.make_plan()
        cases = {
            "wrong serial": dict(serial="SER2"),
            "wrong product": dict(product="shiba"),
            "locked": dict(unlocked="no"),
            "fastbootd": dict(userspace="yes"),
            "size": dict(size=8 * MIB),
        }
        for name, kwargs in cases.items():
            script, log = self.fake_fastboot(**kwargs)
            with patch("builtins.input", return_value="SER1"):
                self.assertEqual(e.main(["apply", "--plan", str(out), "--apply", "--fastboot-bin", str(script)]),
                                 1, name)
            self.assertFalse(log.exists(), name)
        script, log = self.fake_fastboot()
        with patch("builtins.input", return_value="nope"):
            self.assertEqual(e.main(["apply", "--plan", str(out), "--apply", "--fastboot-bin", str(script)]), 1)
        self.assertFalse(log.exists())
        # fastbootd is allowed when asked for explicitly.
        script, log = self.fake_fastboot(userspace="yes")
        self.assertEqual(e.main(["apply", "--plan", str(out), "--apply", "--yes", "--allow-fastbootd",
                                 "--fastboot-bin", str(script)]), 0)
        self.assertTrue(log.exists())

    def test_fetch_records_sizes_and_identity(self):
        script, _ = self.fake_fastboot()
        # fetch answers: write the efs image for `fetch efs`, refuse the rest.
        script.write_text(script.read_text().replace('  " flash"*)',
                          '  " fetch efs "*) cp "%s" "$last";;\n  " fetch"*) echo "FAILED (remote: not allowed)" >&2; exit 1;;\n  " flash"*)'
                          % (self.images / "efs_backup.img")))
        out = self.tmp / "fetched"
        self.assertEqual(e.main(["fetch", "--out", str(out), "--fastboot-bin", str(script)]), 0)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["device"]["serial"], "SER1")
        self.assertEqual(report["sizes"]["efs"], 4 * MIB)
        self.assertEqual(list(report["read"]), ["efs"])
        self.assertIn("efs_backup", report["failed"])
        self.assertEqual((out / "efs.img").read_bytes(), self.good)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
