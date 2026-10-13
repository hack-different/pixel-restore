# pixel-restore

Boot a Tensor Pixel (Pixel 6 and newer) out of **"Pixel ROM Recovery"** mode
and into fastboot, so it can be reflashed normally.

When the boot ROM cannot find a valid bootloader in storage (a failed or
mismatched bootloader flash is the usual cause) the phone shows a black screen
and enumerates over USB as a CDC serial device called *Pixel ROM Recovery*.
Fastboot, adb and the Android Flash Tool cannot see it. The ROM will, however,
accept signed bootloader stages pushed over that serial port, one request at a
time, and once the Android bootloader (ABL) is running the phone comes up in
fastboot. This tool answers those requests directly from the `bootloader.img`
inside the factory image for your model.

It is a Rust reimplementation of the dialogue
[tensor-usbdl](https://github.com/JoshuaDoes/tensor-usbdl) reverse engineered,
with two additions: it reads the FBPK v2 `bootloader.img` (or the whole factory
ZIP) itself instead of needing pre-extracted images, and it is request driven,
so it serves whatever stage the ROM asks for rather than following a fixed
per-model script.

## Build

```sh
cargo build --release
# binary: target/release/pixel-restore
```

macOS needs no drivers or libusb; the ROM is a plain USB serial device.
On Linux, ModemManager may grab the port when it appears; stop it or add a
udev ignore rule for `18d1:4f00`. Build with `--features libudev` if you have
`libudev-dev` and want udev-based port enumeration.

## Supported models

The ROM only accepts a BL1 whose signed header has the "USB bootable" bit set
(see Status). Google's factory images are signed without it, so what decides
whether a model can be rescued is whether such a BL1 exists for its SoC. The
only public ones are in the recovery packs of the
[tensor-usbdl](https://github.com/JoshuaDoes/tensor-usbdl) v0.2.0 release,
which this tool downloads once (SHA-256 pinned), caches, and serves; nothing
in that ZIP is executed. `pixel-restore models` prints this table.

| Model | Codename | SoC | Status |
| --- | --- | --- | --- |
| Pixel 8 | shiba | zuma (G3) | **verified** on a July 2024 bootloader (ripcurrent-14.5) |
| Pixel 8 Pro | husky | zuma (G3) | **verified** before the anti-rollback bump; refused once on ripcurrent-17.0 |
| Pixel 7, 7 Pro | panther, cheetah | gs201 (G2) | recovery pack (`cloudripper` ABL) |
| Pixel 8a | akita | zuma (G3) | husky pack, untested: BL1 key matches, ABL is husky's |
| Pixel 7a, Fold, Tablet | lynx, felix, tangorpro | gs201 (G2) | gs201 pack, untested: ABL is panther/cheetah's |
| Pixel 6, 6 Pro, 6a | oriole, raven, bluejay | gs101 (G1) | no USB-bootable BL1 published |
| Pixel 9 family, 9a | tokay, caiman, komodo, comet, tegu | zumapro (G4) | no USB-bootable BL1 published |
| Pixel 10 family | frankel, blazer, mustang, rango | laguna (G5) | no USB-bootable BL1 published; ROM protocol unknown |

**Anti-rollback is the catch.** The packs' BL1s date from 2023. Once a phone
has run a bootloader that raised its BL1 anti-rollback level, its ROM
refuses them (`bl1 header fail`) and nothing public can boot it over USB.
Observed: a Pixel 8 Pro accepted the husky pack, was reflashed with the
Sep 2026 factory image (ripcurrent-17.0), and now refuses it; a Pixel 8 on
ripcurrent-14.5 (Jul 2024) accepts the shiba pack. Which build first bumped
the level is not known. Updating a rescued phone to the current factory
image closes the door behind it.

"Untested" rows are worth trying: every upload lands in RAM, so a stage the
phone rejects costs nothing. If the sibling's ABL does not run, layer the
model's own factory image under the pack with `--image` and override stages
with `--stage`. For the "no BL1" rows the tool still tries the retail image,
and the ROM will answer `bl1 header fail`.

## Use

1. Unplug the phone. Hold **Power + Volume Up + Volume Down**, plug in USB and
   keep holding for about 15 seconds. `pixel-restore detect` should print the
   serial port.
2. Boot it:

   ```sh
   pixel-restore boot --device husky --wait      # or "Pixel 8 Pro"
   pixel-restore boot --wait                     # let the ROM's chip id pick the pack
   ```

   `--device` picks the recovery pack and makes the tool refuse to send
   anything if the ROM reports a different SoC (a Pixel 9 image offered to a
   Pixel 8, say). Without it the ROM's chip id decides, which works when one
   pack covers the SoC (gs201); for zuma it asks which model. `--wait` keeps
   scanning until the phone appears, so you can start the tool first.

   `--pack auto` (the default) adds the pack when you name `--device` or give
   no other source; with `--image`, `--dir` or `--stage` and no `--device`
   it serves exactly what you gave.

   Midway, after BL2, the phone re-enumerates and BL2 carries on asking for
   GSA, ABL, TZSW, LDFW and BL31 on the new connection; the tool follows it
   (`--reconnect-wait`, default 10 s) and finishes when the phone leaves ROM
   mode, then checks `fastboot devices`.
3. The bootloader now running lives in RAM only. Make it permanent from the
   factory image:

   ```sh
   pixel-restore fetch husky --full              # whole factory ZIP, SHA-256 checked
   unzip husky-*-factory-*.zip && cd husky-*/
   fastboot getvar battery-voltage               # want > 4200 mV before flashing
   fastboot flash bootloader bootloader-*.img
   fastboot reboot-bootloader
   ./flash-all.sh                                # wipes data; drop -w to keep it
   ```

   Don't reboot or let the battery die before the bootloader flash: the phone
   goes straight back to ROM recovery.

### Factory images

```sh
pixel-restore fetch komodo            # newest Pixel 9 Pro XL bootloader-*.img, a few MB
pixel-restore fetch komodo --full     # the whole 4 GB factory ZIP, SHA-256 checked, resumable
pixel-restore fetch komodo --list     # every build Google lists for the device
pixel-restore fetch "Pixel 9 Pro XL"  # model names work too
```

The default pulls only `bootloader-*.img` out of the ZIP with HTTP range
requests; `--full` gets you the image for flash-all. Fetching accepts
Google's factory-image terms, the same as clicking Acknowledge on the page.
`--url <zip url>` skips the listing page.

### Other commands

```sh
pixel-restore detect --all                  # list every serial port if the phone isn't found
pixel-restore unpack bootloader-*.img       # show the partitions, verify CRCs
pixel-restore unpack factory.zip -o out/    # extract them as out/<name>.img
pixel-restore models                        # SoC and support status of every Tensor Pixel
pixel-restore boot --device X --verbose     # print every line the ROM sends
pixel-restore boot --pack tensor-usbdl-v0.2.0.zip --device husky   # use a local release copy
pixel-restore boot --pack sources/zuma/husky                      # or one pack directory
pixel-restore boot --image factory.zip --pack none                # retail only (expect bl1 header fail)
pixel-restore boot --image X --stage dpm=my-dpm.img   # override one partition
pixel-restore boot --image X --map NEWSTAGE=gsa       # teach it an unfamiliar request
```

## How the protocol works

Device → host is text, one message per line:

| line | meaning |
| --- | --- |
| `exynos_usb_booting:eub:<serial>` | banner |
| `eub:req:<serial>:<STAGE>` | please send this stage |
| `C` | clear to send the stage just requested |
| `eub:ack:...` / `eub:nak:...` | accepted / refused |
| `<stage> header fail` | signature or version check failed |
| `irom_booting_failure:...` | failure trace |

Host → device uploads are DNW frames: `ESC D N W`, a little-endian u32 of the
total frame length, the payload, and a 16-bit trailer. tensor-usbdl sends
`FF FF` as the trailer and the ROM accepts it, so that is the default here
(`--checksum sum16` sends the classic byte sum instead).

Each stage image in `bootloader.img` is a 4096-byte signed header followed by
a code body. Some requests want the whole image (`BL1`, `DPM`, `GSA1`), others
want the header and the body separately (`ABL` then `ABLB`, `BL2` then
`BL2B`, ...). The one known generational difference is `EPBL`: Pixel 6/7 want
the whole PBL, Pixel 8 and later want the header and then ask for `EPBB`.
The tool reads the SoC from the serial the ROM reports (`9845` gs101, `9855`
gs201, `9865` zuma; `9875` is assumed for zumapro, and anything unknown is
treated like zuma) and `--epbl full|split` overrides the guess.

Observed on a Pixel 8 Pro: `BL1 DPM GCF GCFB EPBL EPBB BL2 BL2B` from the
ROM, then a USB re-enumeration, then `GSA1 GSAF ABL ABLB TZSW TZSB LDFW LDFB
BL31 BL3B` from BL2, then fastboot. While waiting, the ROM repeats its
request and a bare `C` about every 0.8 s and resets USB after a few seconds
without an upload. The tool only sends its newline nudge to a connection
that has said nothing for 1.5 s, so it can't land inside an upload.

If no `dpm` partition exists, 4096 zero bytes are sent, which is what
tensor-usbdl does and what Pixel 7/8 ROMs accept.

The hand-off happens in two phases. Once BL2 is running it takes over USB
and re-enumerates, so the serial port vanishes and comes back, and the
second half of the requests (`GSA1` onward) arrives on the new connection.
`boot` notices the port closing, waits for the phone to reappear as a ROM
device (`--reconnect-wait`, default 15 s) and carries on, so one run takes
the phone all the way to fastboot. When the phone does not come back it is
booting ABL, which is the end of the sequence.

## Status

* Protocol and container parsing are unit tested, and the full boot loop is
  exercised end to end against `tools/fake_rom.py`, which plays a Pixel 8
  style ROM on a pseudo-terminal and byte-checks every upload.
* **Verified on hardware** with the tensor-usbdl recovery packs: a Pixel 8
  Pro (husky) went from ROM Recovery to fastboot and was reflashed normally,
  and a Pixel 8 (shiba) on a July 2024 bootloader took the whole shiba pack
  in one connection and was reflashed from the RAM-booted fastboot. Other
  models: see Supported models. Run with `--verbose` on first use. If the
  ROM asks for a stage the tool does not know, the error names it and
  `--map` lets you serve it without a rebuild.
* **Retail bootloaders are refused.** Confirmed on a Pixel 8 Pro: the ROM
  ACKs the factory image's BL1 and then answers `bl1 header fail`, for both
  the current stable and the newest beta. The ROM only accepts a BL1 whose
  signed header has the "USB bootable" bit (bit 0 of the flags word at
  0x410) set, and retail images are signed without it. Google publishes no
  such images; the community's tensor-usbdl release carries recovery packs
  (`sources/gs201`, `sources/zuma/shiba`, `sources/zuma/husky`) whose images
  all have the bit set (`0x211` on zuma, `0x401` on gs201); `boot` serves
  them by default. Their BL1s
  date from 2023, so a phone that took Google's May 2025 anti-rollback bump
  for Pixel 6/8 may refuse them too; there is no public answer for that case.
  `unpack` prints each image's header tag and flags word, and `boot` warns
  when the BL1 it is about to send has the bit clear. Uploads are RAM-only,
  so trying costs nothing.
* Images are matched to ROM requests by the ASCII tag in their header magic
  (`EPBL`, `BL2`, `GSA1`, `GSAF`, `ABL`, `TZSW`, `LDFW`, `BL31`, `GCF`;
  BL1 carries `APBL`), falling back to partition/file names. That is what
  sorts out factory images (`gsa_bl1` + `gsa`) and tensor-usbdl packs
  (`gsa.img` + `gsaf.img`, which are the other way round from their names).
* The ROM sends its clear-to-send `C` with no line terminator and gives up
  about a second later, so the reader hands a bare `C` through immediately.
* The `fetch` listing parser is tested against a saved copy of the page's
  markup, not the live page; `--url` is the fallback if Google changes it.

### Testing without a phone

```sh
python3 tools/fake_rom.py --image bootloader.img     # prints /dev/pts/N (Linux) or /dev/ttysNNN (macOS)
pixel-restore boot --image bootloader.img --pack none --port /dev/pts/N --verbose

python3 tools/fake_rom.py --dir sources/zuma/husky --serial 09865001deadbeef --handoff-gap 5
pixel-restore boot --device husky --port /dev/ttysNNN --reconnect-wait 3
```

The fake ROM replays the request order captured from a Pixel 8 Pro,
repeats each request until served, byte-checks every upload against the
image or pack, and with `--handoff-gap` (or `--pause-after BL2B --pause 3`)
goes silent after BL2B the way the real phone does when BL2 takes over. On
macOS the `serialport` crate cannot configure a pty, so the tool opens one
as a plain non-blocking file.
### Brick-and-recover test on a real phone

`tools/brick_test.sh` proves the whole loop on hardware: it flashes a
`bootloader.img` with one deliberately corrupted partition into the current
slot, reboots, waits for the phone to drop into USB boot mode, recovers it
with a pack, then reflashes the good image and checks fastboot comes back on
its own. `tools/tamper_fbpk.py` makes the corrupted image (body bytes
flipped, signed header untouched, entry CRC fixed so the flasher takes it).

```sh
cargo build --release
tools/brick_test.sh ~/Downloads/husky-xxx-factory-xxx \
                    ~/Downloads/tensor-usbdl-v0.2.0/sources/zuma/husky abl
```

Nothing is flashed until you type `brick`. Read this before choosing the
partition:

* **`abl` (default) is the low-risk experiment.** BL1, PBL and BL2 stay
  intact. Expect one of two outcomes, both informative: the phone comes back
  to fastboot by itself (the ROM or BL2 fell back to the other slot, so a
  single bad slot is not a brick), or BL2 enters USB boot mode and asks for
  `GSA1` onward, which exercises the tool's second phase. Even in the worst
  case the ROM and BL1 still work, so the pack's BL1 is never needed.
* **`bl1` reproduces ROM Recovery itself**, the state this tool exists for,
  but it is the one-way door. The ROM only accepts a BL1 with the USB-boot
  bit, the only such BL1s are the community pack's 2023 ones, and a ROM that
  has taken an anti-rollback bump refuses them. **This is confirmed:** the
  Pixel 8 Pro that the husky pack rescued was then reflashed with the
  current factory image (ripcurrent-17.0), and a forced ROM-mode test now
  gets `bl1 header fail` on the same pack BL1. On that phone any brick that
  lands back at the ROM is permanent. Before any brick test, force ROM mode
  (button combo, nothing flashed) and confirm `pixel-restore boot` gets past
  BL1; a Pixel 8 still on ripcurrent-14.5 (July 2024) does.
* **`abl` is only low-risk if the BL1 check above passes.** If BL2 drops the
  phone back to the ROM rather than asking over USB itself, the ROM wants
  BL1 again. Recovery also serves the pack's 2023 later stages, which a
  newer BL2 may reject as older than its own; layer the phone's current
  factory image with `--image` if that happens.
* Flashing writes the current slot only. A phone that falls back to the other
  slot has not been bricked; corrupting both slots is what makes a brick
  certain, and the script deliberately does not do that for you.

## Credits

Protocol details come from JoshuaDoes' tensor-usbdl (AGPL-3.0; this is an
independent reimplementation, not a port of its code). The FBPK layout follows
Google's `fbpack.py` / `fbpacktool.py` published on source.android.com. The
USB-bootable recovery packs are not bundled; `boot` downloads JoshuaDoes'
tensor-usbdl v0.2.0 release from GitHub at run time and reads only its
`sources/` images.
