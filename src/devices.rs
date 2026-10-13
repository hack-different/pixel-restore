//! Which Tensor SoC each Pixel uses, what its boot ROM calls itself, and what
//! is known about getting it from ROM recovery to fastboot.
//!
//! The ROM never says which phone it is, only which SoC: the serial in
//! `eub:req:<serial>:...` starts with `0` and the four-digit chip id
//! (`09865001` is zuma). So the SoC is what the tool checks the user's
//! `--device` against, and what picks a recovery pack.

use crate::stages::Generation;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Soc {
    /// Google's platform name, as in the FBPK header and tensor-usbdl's packs.
    pub name: &'static str,
    pub tensor: &'static str,
    /// Chip id the ROM reports (characters 1..5 of its serial), when known.
    pub chip_id: Option<&'static str>,
    pub generation: Generation,
}

pub const GS101: Soc = Soc {
    name: "gs101",
    tensor: "Tensor G1",
    chip_id: Some("9845"),
    generation: Generation::Legacy,
};
pub const GS201: Soc = Soc {
    name: "gs201",
    tensor: "Tensor G2",
    chip_id: Some("9855"),
    generation: Generation::Legacy,
};
pub const ZUMA: Soc = Soc {
    name: "zuma",
    tensor: "Tensor G3",
    chip_id: Some("9865"),
    generation: Generation::Split,
};
pub const ZUMAPRO: Soc = Soc {
    name: "zumapro",
    tensor: "Tensor G4",
    // Not observed on hardware; assumed to continue the sequence.
    chip_id: Some("9875"),
    generation: Generation::Split,
};
pub const LAGUNA: Soc = Soc {
    name: "laguna",
    tensor: "Tensor G5",
    chip_id: None,
    generation: Generation::Split,
};

pub const SOCS: &[Soc] = &[GS101, GS201, ZUMA, ZUMAPRO, LAGUNA];

/// How far a model can get with what is publicly available.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Support {
    /// Booted to fastboot on real hardware with this tool (on a bootloader
    /// older than the anti-rollback bump that makes the ROM refuse the
    /// pack's 2023 BL1).
    Verified,
    /// tensor-usbdl ships a recovery pack built for this exact bootloader.
    Pack,
    /// Same SoC as a pack, so the pack's USB-bootable BL1 is signed with the
    /// right key, but its later stages (ABL in particular) were built for a
    /// sibling model. Worth a try: every upload is RAM-only.
    SiblingPack,
    /// No USB-bootable BL1 for this SoC is public. Retail BL1s are refused
    /// with "bl1 header fail".
    NoBl1,
}

impl Support {
    pub fn label(self) -> &'static str {
        match self {
            Support::Verified => "verified (pre-bump bootloader)",
            Support::Pack => "recovery pack",
            Support::SiblingPack => "sibling pack (untested)",
            Support::NoBl1 => "no USB-bootable BL1 published",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Model {
    pub codename: &'static str,
    pub name: &'static str,
    pub soc: Soc,
    /// The tensor-usbdl pack directory (under `sources/`) to serve.
    pub pack: Option<&'static str>,
    pub support: Support,
}

const fn m(
    codename: &'static str,
    name: &'static str,
    soc: Soc,
    pack: Option<&'static str>,
    support: Support,
) -> Model {
    Model {
        codename,
        name,
        soc,
        pack,
        support,
    }
}

use Support::*;

pub const MODELS: &[Model] = &[
    m("oriole", "Pixel 6", GS101, None, NoBl1),
    m("raven", "Pixel 6 Pro", GS101, None, NoBl1),
    m("bluejay", "Pixel 6a", GS101, None, NoBl1),
    // The gs201 pack's ABL is "cloudripper", the bootloader panther and
    // cheetah share. lynx, felix and tangorpro have bootloaders of their own.
    m("panther", "Pixel 7", GS201, Some("gs201"), Pack),
    m("cheetah", "Pixel 7 Pro", GS201, Some("gs201"), Pack),
    m("lynx", "Pixel 7a", GS201, Some("gs201"), SiblingPack),
    m("felix", "Pixel Fold", GS201, Some("gs201"), SiblingPack),
    m(
        "tangorpro",
        "Pixel Tablet",
        GS201,
        Some("gs201"),
        SiblingPack,
    ),
    // shiba and husky share the "ripcurrent" bootloader; the two packs differ
    // only in their GSA images. akita has its own bootloader, but the husky
    // pack still gets it to fastboot (on akita-14.5): the ROM re-enumerates
    // after BL2B, and the husky ABL reports `product: unknown`. Serving
    // akita's own ABL instead (--image) is untested.
    m("shiba", "Pixel 8", ZUMA, Some("zuma/shiba"), Verified),
    m("husky", "Pixel 8 Pro", ZUMA, Some("zuma/husky"), Verified),
    m("akita", "Pixel 8a", ZUMA, Some("zuma/husky"), Verified),
    m("tokay", "Pixel 9", ZUMAPRO, None, NoBl1),
    m("caiman", "Pixel 9 Pro", ZUMAPRO, None, NoBl1),
    m("komodo", "Pixel 9 Pro XL", ZUMAPRO, None, NoBl1),
    m("comet", "Pixel 9 Pro Fold", ZUMAPRO, None, NoBl1),
    m("tegu", "Pixel 9a", ZUMAPRO, None, NoBl1),
    m("frankel", "Pixel 10", LAGUNA, None, NoBl1),
    m("blazer", "Pixel 10 Pro", LAGUNA, None, NoBl1),
    m("mustang", "Pixel 10 Pro XL", LAGUNA, None, NoBl1),
    m("rango", "Pixel 10 Pro Fold", LAGUNA, None, NoBl1),
];

pub fn model(codename: &str) -> Option<&'static Model> {
    MODELS.iter().find(|m| m.codename == codename)
}

/// A Pixel from before Tensor. These have a Qualcomm SoC, whose boot ROM
/// falls back to Emergency Download mode (EDL, USB 05c6:9008) rather than the
/// Exynos-style USB boot this tool speaks. Leaving EDL needs a Firehose
/// programmer signed for the device, and none has been published for any
/// Pixel, so the tool cannot help them.
#[derive(Debug, Clone, Copy)]
pub struct QualcommModel {
    pub codename: &'static str,
    pub name: &'static str,
    pub chipset: &'static str,
}

const fn q(codename: &'static str, name: &'static str, chipset: &'static str) -> QualcommModel {
    QualcommModel {
        codename,
        name,
        chipset,
    }
}

pub const QUALCOMM_MODELS: &[QualcommModel] = &[
    q("sailfish", "Pixel", "SD821"),
    q("marlin", "Pixel XL", "SD821"),
    q("walleye", "Pixel 2", "SD835"),
    q("taimen", "Pixel 2 XL", "SD835"),
    q("blueline", "Pixel 3", "SD845"),
    q("crosshatch", "Pixel 3 XL", "SD845"),
    q("sargo", "Pixel 3a", "SD670"),
    q("bonito", "Pixel 3a XL", "SD670"),
    q("flame", "Pixel 4", "SD855"),
    q("coral", "Pixel 4 XL", "SD855"),
    q("sunfish", "Pixel 4a", "SD730G"),
    q("bramble", "Pixel 4a 5G", "SD765G"),
    q("redfin", "Pixel 5", "SD765G"),
    q("barbet", "Pixel 5a", "SD765G"),
];

pub fn qualcomm_model(codename: &str) -> Option<&'static QualcommModel> {
    QUALCOMM_MODELS.iter().find(|m| m.codename == codename)
}

pub const QUALCOMM_STATUS: &str = "not supported: Qualcomm EDL, no public signed programmer";

/// The SoC a ROM serial (`09865001...`) belongs to.
pub fn soc_for_serial(serial: &str) -> Option<Soc> {
    let id = serial.get(1..5)?;
    SOCS.iter().copied().find(|s| s.chip_id == Some(id))
}

/// Human-readable family for a SoC, e.g. "Pixel 8, Pixel 8 Pro, Pixel 8a".
pub fn family(soc: Soc) -> String {
    MODELS
        .iter()
        .filter(|m| m.soc == soc)
        .map(|m| m.name)
        .collect::<Vec<_>>()
        .join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serial_maps_to_soc() {
        assert_eq!(soc_for_serial("09865001ae2d5d480874"), Some(ZUMA));
        assert_eq!(soc_for_serial("09855001"), Some(GS201));
        assert_eq!(soc_for_serial("09999001"), None);
        assert_eq!(
            soc_for_serial("09855001").unwrap().generation,
            Generation::Legacy
        );
        assert_eq!(
            soc_for_serial("09875001").unwrap().generation,
            Generation::Split
        );
        assert_eq!(soc_for_serial(""), None);
    }

    #[test]
    fn every_model_has_a_known_codename_and_pack_matches_soc() {
        for m in MODELS {
            assert!(crate::fetch::DEVICES.iter().any(|(c, _)| *c == m.codename));
            if let Some(p) = m.pack {
                assert!(p.starts_with(m.soc.name), "{} uses {p}", m.codename);
            }
            assert_eq!(m.pack.is_none(), m.support == NoBl1);
        }
        assert_eq!(family(ZUMA), "Pixel 8, Pixel 8 Pro, Pixel 8a");
    }

    #[test]
    fn qualcomm_pixels_are_known_but_not_tensor() {
        assert_eq!(qualcomm_model("sunfish").unwrap().name, "Pixel 4a");
        for q in QUALCOMM_MODELS {
            assert!(
                model(q.codename).is_none(),
                "{} is in both lists",
                q.codename
            );
        }
        assert_eq!(crate::fetch::codename("Pixel 4a").unwrap(), "sunfish");
    }
}
