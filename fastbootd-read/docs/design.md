# Read-enabled fastbootd: design

## The problem

`pixel-restore modem read-partitions --fastboot` pulls small partitions
(`devinfo`, `efs`, `efs_backup`, `persist`, `modem_userdata`, the boot chain)
with `fastboot fetch <name>` so the owner can inspect and back them up for
IMEI repair without a rooted Android. On komodo (Pixel 9 Pro XL) every fetch
failed. Stock fastbootd allows reading only three partitions.

## Why stock fastbootd refuses

The device-side `fetch` handler lives in AOSP
`system/core/fastboot/device/commands.cpp`. `PartitionFetcher::Fetch()` gates it
three ways, in order:

1. **Build gate.** `kEnableFetch` is true only when the binary is compiled with
   `-DFB_ENABLE_FETCH`, which `system/core/fastboot/Android.bp` adds only on a
   `debuggable` (userdebug/eng) build. A stock `user` build answers
   `Fetch is not allowed on user build`.
2. **Lock gate.** `GetDeviceLockStatus()` returns true unless
   `ro.boot.verifiedbootstate == "orange"`. A locked phone answers
   `Fetch is not allowed on locked devices`. Unlocked reads are allowed.
3. **Allow-list gate.** A hard-coded three-entry list:

   ```cpp
   static constexpr std::array<const char*, 3> kAllowedPartitions{
       "vendor_boot", "vendor_boot_a", "vendor_boot_b",
   };
   ```

   Anything else answers `Fetch is only allowed on [...]`. There is also a
   256 MiB per-transfer cap (`kMaxFetchSizeDefault`).

(The bare "Command failed" komodo returned earlier came from the **bootloader**
fastboot, which has no `fetch` at all. The gating above is what `fastbootd`
enforces; this project is about `fastbootd`.)

## What this changes

A run-time **fetch policy** replaces the hard-coded allow-list, with three
levels, all **read only** — no write, flash or erase path is touched:

| Policy | Menu entry | Effect |
| --- | --- | --- |
| `kFetchPolicyStock` (0) | — (default) | upstream: only the three-entry allow-list |
| `kFetchPolicyReadAll` (1) | **Enable reading** | fetch any partition fastbootd can open by name; size cap kept |
| `kFetchPolicyUnrestricted` (2) | **Enable all partitions** | also lifts the per-transfer size cap |

The default is always stock. The policy can only be widened from the phone's
**on-screen fastboot menu**, each step behind a warning and a Cancel/confirm
screen. Nothing over USB can change it — the host can only read what the owner
has already unlocked and enabled on the device.

## Hashing on the phone

`fastboot oem sha256 <partition>` (also `sha1`, `sha512`) makes fastbootd read
the whole partition itself and send back only the digest:

```
(bootloader) sha256 <lowercase hex> <partition> <size in bytes>
```

So a large partition can be compared against a backup or a factory image
without pulling it over USB. Progress lines arrive every GiB for big ones.

It uses the same gates as `fetch`, with no new toggle:
- refused on a locked bootloader;
- the stock allow-list (`vendor_boot*`) can always be hashed;
- any other partition needs policy **Read all** or higher, i.e. the owner has
  picked "Enable reading" (or "Enable all partitions") on the screen;
- no size cap, since nothing is transferred;
- read only: the partition is opened `O_RDONLY` through the same
  `OpenPartition` path `fetch` uses, so logical partitions inside `super` work
  too (use slot suffixes for slotted names, e.g. `boot_a`).

The command is handled in fastbootd before the OEM command reaches the fastboot
HAL. Any other `oem` command still goes to the HAL unchanged.

## How the toggle reaches the daemon

`fastbootd` (the USB daemon, `system/core`) and the recovery UI that draws the
fastboot menu (`bootable/recovery/fastboot/fastboot.cpp`) are **separate
processes** sharing one recovery ramdisk. They communicate through a tiny
tmpfs state file, `/dev/fastbootd_fetch_policy`:

- The recovery menu (writer) writes `1` or `2` after the user confirms.
- `fastbootd` (reader, `GetFetchPolicy()` in the new `fetch_policy.cpp`) reads
  it before every fetch. Any missing/garbled value reads back as `0` (stock),
  so the safe default always wins.

No command can change the policy, so it is unreachable from the host (the
`oem sha*` commands only read it). Building the recovery/fastbootd as **userdebug** both compiles `fetch`
in (gate 1) and keeps the two recovery-domain processes able to share the
tmpfs file.

## Files

- New: `system/core/fastboot/device/fetch_policy.{h,cpp}` — the policy enum,
  the shared path, and the fail-safe reader.
- New: `system/core/fastboot/device/partition_hash.{h,cpp}` — `oem sha256|sha1|sha512`.
- Patched: `system/core/fastboot/device/commands.cpp` — the three gates become
  policy-aware, and `OemCmdHandler` tries the hash command before the HAL;
  `system/core/fastboot/Android.bp` — builds the two new sources and links
  `libcrypto` (BoringSSL) into fastbootd.
- Patched: `bootable/recovery/fastboot/fastboot.cpp` — two confirmed menu
  entries that widen the policy and then return to the menu (fastbootd keeps
  serving).

## What is verified vs. not

- **Verified here:** the gating analysis against real upstream source, and that
  both patches apply cleanly (`git apply --check`) to their stated bases
  (`system/core` @ AOSP `main`; `bootable/recovery` @ GrapheneOS `17`).
- **Not done here (needs the komodo + a Linux builder):** compiling, and
  confirming on-device that `fastboot boot` enters the modified fastbootd. See
  `build-and-boot.md`. This container has no Android build tree, no USB, and
  cannot reach the phone.
