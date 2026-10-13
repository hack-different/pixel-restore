# Migrating restore_pixel_tensor

`hack-different/pixel-restore` is the canonical implementation. The original
`restore_pixel_tensor` repository is included under
[`legacy/restore_pixel_tensor`](../legacy/restore_pixel_tensor), with both original
commits retained as ancestors of the integration commit. The imported files are
unchanged. They are historical reference code, outside the active Cargo package;
the merge does not add their dependencies or change their licensing status.

## Serving existing request files

The prototype selected a file named `<STAGE>.bin` for each `eub:req` message.
Use the new `boot --firmware` option for this exact-request workflow:

```sh
pixel-restore boot --firmware ./firmware --port /dev/cu.usbmodem1234
# -f is an alias for --firmware
```

File stems are request names: `BL1.bin`, `EPBL.bin`, `ABL.bin`, `ABLB.bin`, etc.
Names and `.bin` extensions are matched case-insensitively. Each file is the
complete payload for that request and is sent byte-for-byte: a pre-split
`ABLB.bin` body is not sliced a second time. An unknown request can be served
when its exact file is present. A missing, empty, invalidly named, or ambiguous
payload produces an error. Non-`.bin` files and subdirectories are ignored.

This mode is exclusive. It cannot be combined with `--image`, `--device`,
`--pack`, `--dir`, `--stage`, or `--map`. It does not fetch recovery packs or
substitute synthesized DPM data for a missing file. Use only payloads already
prepared for the connected device and request. The tool does not establish their
model compatibility; the existing ROM signature and rollback checks still apply.

For full partition images, continue to use `--dir` or `--image` instead. Those
modes perform the established header/body slicing and recovery-pack selection.

The old standalone command's `-d`/`--device` named a serial port. In this CLI,
`--port` names the port; `--device` names a phone model. The old command itself
and its argument layout are not installed as a second executable.

## Transport and parser differences

The prototype attempted an XMODEM upload at 9600 baud, read the entire stream
until EOF, and parsed the third request field as an `i64` address. The active
implementation treats that field as the ROM serial/chip identifier and reads
incremental messages, waiting for clear-to-send before transmitting DNW frames
on the existing 115200-baud serial configuration. It can serve multiple requests
and reconnect across USB re-enumeration.

The XMODEM transport is preserved only in the historical source. This merge
adds exact-file input to the current DNW transport; it does not claim XMODEM
hardware compatibility or reintroduce the numeric-address parsing assumption.

## Validation

Unit tests cover filename validation, CLI conflicts, exact header/body payloads,
missing-file failures, and a simulated ROM exchange that verifies the transmitted
DNW frame and unchanged payload. No physical device was flashed or used to
validate the new input mode.
