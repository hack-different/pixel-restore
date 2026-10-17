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

## Use

1. Get the **newest factory image for the exact model**. Either download it
   from <https://developers.google.com/android/images>, or let the tool do it:

   ```sh
   pixel-restore fetch komodo            # newest Pixel 9 Pro XL bootloader-*.img, a few MB
   pixel-restore fetch komodo --full     # the whole 4 GB factory ZIP, SHA-256 checked, resumable
   pixel-restore fetch komodo --list     # every build Google lists for the device
   pixel-restore fetch "Pixel 9 Pro XL"  # model names work too
   ```

   The default pulls only `bootloader-*.img` out of the ZIP with HTTP range
   requests, which is all `boot` needs; `--full` gets you the image for
   flash-all afterwards. Fetching accepts Google's factory-image terms, the
   same as clicking Acknowledge on the page. `--url <zip url>` skips the
   listing page. "bl1 header fail" or a NAK from the ROM almost always means
   the wrong model's image, or one older than the anti-rollback level the
   phone already has.
2. Unplug the phone. Hold **Power + Volume Up + Volume Down**, plug in USB and
   keep holding for about 15 seconds. `pixel-restore detect` should print the
   serial port.
3. Run the boot:

   ```sh
   pixel-restore boot --image komodo-xxxx-factory-xxxx.zip --wait
   ```

   `--image` takes either the factory ZIP or the `bootloader-*.img` from
   inside it; `--device komodo` fetches the newest bootloader instead.
   `--wait` keeps scanning until the phone appears, so you can start the
   tool first and then do the button dance.
4. When the ROM stops asking for stages the phone is booting ABL. Make it
   permanent from the factory image folder:

   ```sh
   fastboot devices
   fastboot getvar battery-voltage       # want > 4200 mV before flashing
   fastboot flash bootloader bootloader-*.img
   fastboot reboot-bootloader
   ./flash-all.sh
   ```

Pushing stages over USB only boots the phone once; step 4 is what fixes it.

### Other commands

```sh
pixel-restore detect --all                  # list every serial port if the phone isn't found
pixel-restore unpack bootloader-*.img       # show the partitions, verify CRCs
pixel-restore unpack factory.zip -o out/    # extract them as out/<name>.img
pixel-restore boot --image X --verbose      # print every line the ROM sends
pixel-restore boot --dir tensor-usbdl-v0.2.0/sources/zuma/husky   # serve a recovery pack
pixel-restore boot --image X --stage dpm=my-dpm.img   # override one partition
pixel-restore boot --image X --map NEWSTAGE=gsa       # teach it an unfamiliar request
```

## Repairing the modem (blank IMEI, no baseband)

If the phone boots but shows no IMEI, "Unknown" baseband or no signal after a
flash, the usual cause is a radio image from a different build than the
bootloader, or a damaged modem partition. The IMEI itself lives in the
phone's `efs`/`persist` data, which factory images never touch, so it is
normally still there. With the phone in fastboot and the bootloader unlocked:

```sh
pixel-restore repair-modem --image komodo-xxxx-factory-xxxx.zip   # radio from the same build as the bootloader
pixel-restore repair-modem --device komodo --build cp3a.260905.009  # fetch just radio-*.img for that build
pixel-restore repair-modem --image radio-komodo-xxxx.img --dry-run  # show what it would do
pixel-restore repair-modem --restore efs=efs.img --restore persist=persist.img   # this phone's own backups
```

It prints the phone's product, bootloader and baseband, refuses a radio
built for another model, warns when the ZIP's bootloader differs from the
one on the phone, asks before flashing (`--yes` skips that), flashes `radio`,
reboots into the bootloader and reads the baseband back. `--restore` only
writes `efs`, `efs_backup`, `modem_userdata` and `persist`, and only makes
sense with images taken from the same phone. `--serial` is needed only when
more than one phone is in fastboot; `$FASTBOOT` points it at a specific
`fastboot` binary.

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
The tool guesses from the serial the ROM reports (`9845` Pixel 6, `9855`
Pixel 7, `9865` Pixel 8; anything newer is treated like Pixel 8) and
`--epbl full|split` overrides the guess.

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
* **Verified on a Pixel 8 Pro** (husky), which went from ROM Recovery to
  fastboot with the tensor-usbdl husky pack and was then reflashed normally.
  The stage table comes from that run plus Pixel 7/8 observations; a Pixel 9
  (zumapro) ROM is assumed to behave like Pixel 8. Run with `--verbose` on
  first use. If it asks for a stage the tool does not know, the error names
  it and `--map` lets you serve it without a rebuild.
* **Retail bootloaders are refused.** Confirmed on a Pixel 8 Pro: the ROM
  ACKs the factory image's BL1 and then answers `bl1 header fail`, for both
  the current stable and the newest beta. The ROM only accepts a BL1 whose
  signed header has the "USB bootable" bit (bit 0 of the flags word at
  0x410) set, and retail images are signed without it. Google publishes no
  such images; the community's tensor-usbdl release carries recovery packs
  (`sources/gs201`, `sources/zuma/shiba`, `sources/zuma/husky`) whose images
  all have flags `0x211`. Serve one with `boot --dir <pack dir>`. Their BL1s
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
pixel-restore boot --image bootloader.img --port /dev/pts/N --verbose
```

`fake_rom.py --pause-after BL2B --pause 3` goes quiet for a few seconds after
that stage, the way the real phone does while BL2 re-enumerates, to exercise
the reconnect path (`boot --idle-timeout 1 --reconnect-wait 5` keeps the
test quick).

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
  has taken an anti-rollback bump refuses them. The husky pack was accepted
  by this phone before it was reflashed with the current factory image; it is
  not known whether that image bumped the BL1 level. If it did, a corrupted
  BL1 cannot be recovered by anyone. Only do this on a phone you can afford
  to lose, or after confirming the pack still boots the phone from a state
  that does not depend on it.
* Flashing writes the current slot only. A phone that falls back to the other
  slot has not been bricked; corrupting both slots is what makes a brick
  certain, and the script deliberately does not do that for you.

## Credits

Protocol details come from JoshuaDoes' tensor-usbdl (AGPL-3.0; this is an
independent reimplementation, not a port of its code). The FBPK layout follows
Google's `fbpack.py` / `fbpacktool.py` published on source.android.com.
