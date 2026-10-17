#!/usr/bin/env bash
# Build a trusted komodo (zuma pro) device tree blob from kernel sources,
# with our overrides layered on, for the CONFIG_PIXEL_TRUSTED_DTB patch to
# .incbin. No device access; pure host-side compile.
#
# Prereqs: the zumapro kernel source tree checked out (the same ACK
# android14-6.1 tree used for the fastbootd-read build), and dtc + cpp.
#
# Usage:
#   build_trusted_dtb.sh <kernel-src-dir> [out-dir]
#
# Output: <out-dir>/pixel-trusted.dtb  (copy to arch/arm64/boot/ in the
# kernel tree before building with CONFIG_PIXEL_TRUSTED_DTB=y).
#
# Approach: our overrides are appended to the base .dts BEFORE preprocessing,
# so the source's &labels (e.g. &spi_11, &hsi2c_8) are in scope and our
# fragments compose by phandle. We do NOT round-trip through dtb first,
# because dtc -I dtb loses the symbol/label table and the &label references
# would then fail to resolve.
set -euo pipefail

KSRC="${1:?usage: build_trusted_dtb.sh <kernel-src-dir> [out-dir]}"
OUT="${2:-$(pwd)}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo ">> locating komodo base .dts under $KSRC"
BASE_DTS="$(find "$KSRC" -path '*dts*' -name '*komodo*.dts' 2>/dev/null | head -n1 || true)"
if [ -z "$BASE_DTS" ]; then
  echo "!! no komodo .dts found. zumapro .dts candidates present:" >&2
  find "$KSRC" -path '*dts*zumapro*' -name '*.dts' 2>/dev/null | head >&2 || true
  echo "   Set BASE_DTS by hand (export BASE_DTS=...) and re-run." >&2
  exit 2
fi
echo ">> base: $BASE_DTS"

INCDIRS=$(find "$KSRC" -path '*dts*' -type d 2>/dev/null \
          | sed 's/^/-I /' | tr '\n' ' ')

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Compose: base .dts, then our overrides appended as trailing /{...} and
# &label {...} fragments. Keep base's own #include lines working by running
# cpp over the concatenation from the base file's directory.
cp "$BASE_DTS" "$WORK/trusted.dts"
{
  echo ''
  echo '/* ---- pixel-restore trusted overrides ---- */'
  cat "$HERE/komodo_overrides.dtsi"
} >> "$WORK/trusted.dts"

echo ">> preprocessing"
cpp -nostdinc -I "$KSRC/include" -I "$(dirname "$BASE_DTS")" $INCDIRS \
    -undef -x assembler-with-cpp "$WORK/trusted.dts" > "$WORK/trusted.pp.dts"

echo ">> compiling pixel-trusted.dtb"
dtc -I dts -O dtb -@ -o "$OUT/pixel-trusted.dtb" "$WORK/trusted.pp.dts"

echo ">> wrote $OUT/pixel-trusted.dtb"
echo "   sha256: $(sha256sum "$OUT/pixel-trusted.dtb" | cut -d' ' -f1)"
echo "   size:   $(stat -c%s "$OUT/pixel-trusted.dtb") bytes (must be < 2 MiB)"
echo
echo "Next: copy to \$KSRC/arch/arm64/boot/pixel-trusted.dtb and build the"
echo "kernel with CONFIG_PIXEL_TRUSTED_DTB=y (see ../patches and ../README.md)."
