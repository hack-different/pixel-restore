# Firmware from OTA packages

`tools/pixel_ota.py` is the OTA counterpart of `tools/pixel_gsc_fetch.py`.
Factory ZIPs carry the GSC firmware and bootloader/radio packs as plain ZIP
members; an OTA ZIP carries one `payload.bin` (an update_engine A/B payload)
whose protobuf manifest lists every partition with its size and SHA-256 and
the operations that write it. Google's OTA page publishes full OTAs, where
every operation is a REPLACE (raw, bzip2, xz or zstd) or ZERO, so any byte
range of any partition can be rebuilt from just the blobs covering it.

`payload.bin` is stored uncompressed in the OTA ZIP, so everything works by
HTTP range on dl.google.com:

| Command | What is transferred |
|---|---|
| `list <url>` / `listings` | ZIP tail + manifest (well under 1 MB): every partition's size and SHA-256 |
| `extract <url> --partition abl` | that partition's blobs only, checked against the manifest SHA-256 |
| `fetch --kinds gsc` | vendor filesystem metadata and the `/firmware` GSC files' blocks only |

## Running it (on the Mac; dl.google.com is blocked from the cloud)

```sh
python3 tools/pixel_ota.py index --out ota-index.json --gsc-only
python3 tools/pixel_ota.py list "$(jq -r '.by_codename.komodo.images[-1].url' ota-index.json)"
python3 tools/pixel_ota.py listings --index ota-index.json --out ota-listings --device komodo
python3 tools/pixel_ota.py fetch --index ota-index.json --out gsc-out --device komodo --limit 2
python3 tools/pixel_gsc_fetch.py report --out gsc-out
```

`fetch` writes the same `manifest.json` layout as `pixel_gsc_fetch.py fetch`
(`images` keyed by ZIP file name; `files` with `path`, `kind`, `size`,
`sha256`, and Titan header/tag fields for GSC blobs), plus `source: "ota"`,
`partition`, and for whole partitions `manifest_sha256` / `verified`. OTA
blobs land under `<codename>/<build>/payload.bin/...`, so the factory and OTA
tools can share one `--out` folder (run them one at a time) and `report`
groups both, deduplicated by SHA-256.

Kinds: `bootloader` is every non-OS partition (abl, bl1, bl2, bl31, gsa,
ldfw, tzsw, xbl, aop, ...), `radio` is `modem`, `gsc` is any GSC-named file
under `/firmware` in the vendor partition (ext4 or EROFS via
`android_fs.py`). `--partition NAME` pulls any other partition whole.

Incremental OTAs (SOURCE_COPY / *DIFF operations) are refused with a note;
they need the source build's partitions.
