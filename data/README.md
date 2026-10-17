# Factory image index

Produced by `tools/pixel_gsc_fetch.py index --out data/firmware-index.json
--devices-out data/device-index.json` from Google's factory image listing
(https://developers.google.com/android/images), which is where the SHA-256s
come from.

- `firmware-index.json`: every factory image URL by product codename, newest
  release last, with build id, SHA-256, description and carrier flag.
- `device-index.json`: one row per codename: marketing name, family, SoC,
  platform, release year, GSC chip (Titan M / Titan M2) and known flavor,
  image count, first and latest release. Rows marked `inferred` are from the
  family, not confirmed.
- `listings/<codename>/<build>.json` (not committed yet): the complete file
  list of each image, from `pixel_gsc_fetch.py listings`.
