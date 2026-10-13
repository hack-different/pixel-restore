//! Mapping from the stage names the boot ROM asks for (`BL1`, `EPBL`, `ABLB`,
//! ...) to partitions inside `bootloader.img`, plus which slice of that
//! partition to send.
//!
//! Every stage image in the pack is a 4096-byte signed header followed by a
//! code body. The ROM sometimes asks for the whole image in one request and
//! sometimes for the header and body separately (the body request is the
//! header's name with a trailing `B`: `ABL` then `ABLB`, `BL2` then `BL2B`).
//! The split table below is the order observed by tensor-usbdl on Pixel 7 and
//! Pixel 8 hardware; a name the table does not know falls through to a
//! best-effort guess so a newer SoC can still be served.

use crate::fbpk::Pack;
use anyhow::{anyhow, Result};
use std::collections::HashMap;

pub const HEADER_LEN: usize = 4096;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Part {
    Full,
    Header,
    Body,
}

/// Which SoC generation the connected ROM belongs to. Decides the one known
/// per-generation difference: Pixel 7 (gs201) wants the whole PBL when it asks
/// for `EPBL`, Pixel 8 (zuma) wants only the header and then asks for `EPBB`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Generation {
    /// Pixel 6 / 7 style: `EPBL` is the whole PBL.
    Legacy,
    /// Pixel 8 and later: `EPBL` is the header, `EPBB` the body.
    Split,
}

/// Resolve a ROM request to (partition name, part of the image).
pub fn resolve(stage: &str, generation: Generation) -> Option<(&'static str, Part)> {
    use Part::*;
    Some(match stage {
        "BL1" => ("bl1", Full),
        "DPM" => ("dpm", Full),
        "EPBL" => (
            "pbl",
            if generation == Generation::Split {
                Header
            } else {
                Full
            },
        ),
        "EPBB" => ("pbl", Body),
        "BL2" => ("bl2", Header),
        "BL2B" => ("bl2", Body),
        "GSA1" => ("gsa_bl1", Full),
        "GSAF" => ("gsa", Full),
        "ABL" => ("abl", Header),
        "ABLB" => ("abl", Body),
        "TZSW" => ("tzsw", Header),
        "TZSB" => ("tzsw", Body),
        "LDFW" => ("ldfw", Header),
        "LDFB" => ("ldfw", Body),
        "BL31" => ("bl31", Header),
        "BL3B" => ("bl31", Body),
        "GCF" => ("gcf", Header),
        "GCFB" => ("gcf", Body),
        _ => return None,
    })
}

/// Alternative partition names to try, in order. Factory images carry the
/// GSA (security chip) first stage as `gsa_bl1` and its firmware as `gsa`;
/// tensor-usbdl's loose packs have `gsa.img` (sent for GSA1) and `gsaf.img`
/// (sent for GSAF), and the order below reproduces that for both layouts.
fn aliases(partition: &str) -> &'static [&'static str] {
    match partition {
        "gsa_bl1" => &["gsa_bl1", "gsa1", "gsa"],
        "gsa" => &["gsaf", "gsa"],
        "pbl" => &["pbl", "epbl"],
        _ => &[],
    }
}

/// The few header fields tensor-usbdl documented in a stage image: a magic
/// word at 0x400, the body length at 0x40C and a flags word at 0x410 whose
/// low byte is the "USB bootable" bit the Pixel 6 ROM insists on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HeaderInfo {
    pub magic: u32,
    pub body_len: u32,
    pub flags: u32,
}

/// The ASCII tag in the header's magic word, which names the ROM request
/// the image answers: `EPBL`, `BL2`, `GSA1`, `GSAF`, `ABL`, `TZSW`, `LDFW`,
/// `BL31`, `GCF`; BL1 carries `APBL`. Images are matched on this first, so
/// a file's name does not have to be right.
pub fn header_tag(image: &[u8]) -> Option<String> {
    let info = header_info(image)?;
    let raw = info.magic.to_le_bytes();
    let end = raw.iter().position(|&b| b == 0).unwrap_or(4);
    let tag: String = raw[..end].iter().map(|&b| b as char).collect();
    if tag.is_empty() || !tag.chars().all(|c| c.is_ascii_alphanumeric()) {
        return None;
    }
    Some(tag.to_ascii_uppercase())
}

/// Header tag the ROM request `stage` is served from (a body request wants
/// the body of the image whose header carries the base tag).
fn tag_for(stage: &str) -> &str {
    match stage {
        "BL1" => "APBL",
        "EPBB" => "EPBL",
        "BL2B" => "BL2",
        "ABLB" => "ABL",
        "TZSB" => "TZSW",
        "LDFB" => "LDFW",
        "BL3B" => "BL31",
        "GCFB" => "GCF",
        other => other,
    }
}

pub fn header_info(image: &[u8]) -> Option<HeaderInfo> {
    if image.len() < HEADER_LEN {
        return None;
    }
    let u32_at = |o: usize| u32::from_le_bytes(image[o..o + 4].try_into().unwrap());
    Some(HeaderInfo {
        magic: u32_at(0x400),
        body_len: u32_at(0x40c),
        flags: u32_at(0x410),
    })
}

/// Where stage bytes come from: the FBPK pack, loose files, or synthesized.
pub struct Sources {
    pub pack: Option<Pack>,
    /// Loose image files keyed by lowercase partition name (`bl1`, `abl`, ...).
    pub files: HashMap<String, Vec<u8>>,
    /// Explicit `REQUEST=partition` overrides from the command line.
    pub remap: HashMap<String, String>,
}

impl Sources {
    /// Raw bytes of a partition, if any source has it.
    pub fn partition(&self, partition: &str) -> Option<&[u8]> {
        self.partition_bytes(partition)
    }

    /// Images whose header magic names `tag`: loose files (recovery packs,
    /// --dir, --stage) first, then the FBPK.
    fn tagged(&self, tag: &str) -> [Vec<(String, &[u8])>; 2] {
        let hit = |b: &[u8]| header_tag(b).as_deref() == Some(tag);
        let files = self
            .files
            .iter()
            .filter(|(_, b)| hit(b))
            .map(|(n, b)| (format!("{n}.img"), b.as_slice()))
            .collect();
        let pack = self
            .pack
            .iter()
            .flat_map(|p| {
                p.data_entries()
                    .map(move |e| (e.name.clone(), p.entry_data(e)))
            })
            .filter(|(_, b)| hit(b))
            .collect();
        [files, pack]
    }

    /// The one image whose header magic names `tag`, if exactly one does.
    /// Loose files are searched before the FBPK, so a recovery pack layered
    /// over a factory image wins stage by stage.
    fn by_tag(&self, tag: &str) -> Option<(String, &[u8])> {
        for mut hits in self.tagged(tag) {
            match hits.len() {
                0 => continue,
                1 => return hits.pop(),
                _ => return None, // ambiguous; let the name lookup decide
            }
        }
        None
    }

    fn partition_bytes(&self, partition: &str) -> Option<&[u8]> {
        let names: Vec<&str> = if aliases(partition).is_empty() {
            vec![partition]
        } else {
            aliases(partition).to_vec()
        };
        // Loose files win over the pack, with the same alias order, so a
        // tensor-usbdl style directory (gsa.img + gsaf.img) is served the way
        // that tool serves it.
        if let Some(bytes) = names.iter().find_map(|n| self.files.get(*n)) {
            return Some(bytes);
        }
        let pack = self.pack.as_ref()?;
        names
            .iter()
            .find_map(|n| pack.find(n))
            .map(|e| pack.entry_data(e))
    }

    /// Produce the exact bytes to upload for a ROM request. Returns the
    /// payload plus a human-readable description of where it came from.
    pub fn payload_for(&self, stage: &str, generation: Generation) -> Result<(Vec<u8>, String)> {
        let (partition, part) = match self.remap.get(stage) {
            Some(p) => (
                p.as_str(),
                resolve(stage, generation)
                    .map(|r| r.1)
                    .unwrap_or(Part::Full),
            ),
            None => resolve(stage, generation).ok_or_else(|| {
                anyhow!(
                    "the ROM asked for '{stage}', which I don't know how to serve; \
                     pass --map {stage}=<partition> to tell me which image it wants"
                )
            })?,
        };

        let tagged = if self.remap.contains_key(stage) {
            None
        } else {
            self.by_tag(tag_for(stage))
        };
        let (source_name, bytes) = match tagged {
            Some((name, b)) => (format!("{name}, header tag {}", tag_for(stage)), b),
            None => match self.partition_bytes(partition) {
                Some(b) => (partition.to_string(), b),
                None if partition == "dpm" => {
                    // tensor-usbdl sends 4 KiB of zeros when no DPM image exists
                    // and Pixel 7/8 ROMs accept it, so do the same.
                    return Ok((vec![0u8; HEADER_LEN], "zeroed 4096-byte DPM".into()));
                }
                None => {
                    return Err(anyhow!(
                        "no '{partition}' image available for request '{stage}' \
                         (not in bootloader.img and no --stage {partition}=<file> given)"
                    ))
                }
            },
        };

        let desc_part = match part {
            Part::Full => "full image",
            Part::Header => "4096-byte header",
            Part::Body => "body after header",
        };
        let slice = match part {
            Part::Full => bytes,
            Part::Header => bytes
                .get(..HEADER_LEN)
                .ok_or_else(|| anyhow!("'{partition}' is shorter than its 4096-byte header"))?,
            Part::Body => bytes
                .get(HEADER_LEN..)
                .ok_or_else(|| anyhow!("'{partition}' has no body after its header"))?,
        };
        Ok((
            slice.to_vec(),
            format!("{source_name} ({desc_part}, {} bytes)", slice.len()),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fbpk::{self, ENTRY_PARTITION_DATA};

    fn sources() -> Sources {
        let mut abl = vec![0xAAu8; HEADER_LEN];
        abl.extend_from_slice(&[0xBB; 100]);
        let img = fbpk::tests::build(&[
            ("bl1_a", ENTRY_PARTITION_DATA, true, &[1u8; HEADER_LEN + 8]),
            ("abl_a", ENTRY_PARTITION_DATA, true, &abl),
            ("gsa_bl1", ENTRY_PARTITION_DATA, false, &[7u8; 10]),
            ("gsa", ENTRY_PARTITION_DATA, false, &[8u8; 12]),
            ("pbl", ENTRY_PARTITION_DATA, false, &[9u8; HEADER_LEN + 3]),
        ]);
        Sources {
            pack: Some(Pack::parse(img).unwrap()),
            files: HashMap::new(),
            remap: HashMap::new(),
        }
    }

    #[test]
    fn serves_full_header_and_body() {
        let s = sources();
        let (bl1, _) = s.payload_for("BL1", Generation::Split).unwrap();
        assert_eq!(bl1.len(), HEADER_LEN + 8);
        let (hdr, _) = s.payload_for("ABL", Generation::Split).unwrap();
        assert_eq!(hdr.len(), HEADER_LEN);
        assert!(hdr.iter().all(|&b| b == 0xAA));
        let (body, _) = s.payload_for("ABLB", Generation::Split).unwrap();
        assert_eq!(body, vec![0xBB; 100]);
    }

    #[test]
    fn epbl_depends_on_generation() {
        let s = sources();
        assert_eq!(
            s.payload_for("EPBL", Generation::Split).unwrap().0.len(),
            HEADER_LEN
        );
        assert_eq!(
            s.payload_for("EPBL", Generation::Legacy).unwrap().0.len(),
            HEADER_LEN + 3
        );
        assert_eq!(s.payload_for("EPBB", Generation::Split).unwrap().0.len(), 3);
    }

    #[test]
    fn gsa_alias_and_dpm_fallback() {
        let s = sources();
        assert_eq!(
            s.payload_for("GSA1", Generation::Split).unwrap().0,
            vec![7u8; 10]
        );
        assert_eq!(
            s.payload_for("GSAF", Generation::Split).unwrap().0,
            vec![8u8; 12]
        );
        let (dpm, desc) = s.payload_for("DPM", Generation::Split).unwrap();
        assert_eq!(dpm, vec![0u8; HEADER_LEN]);
        assert!(desc.contains("zeroed"));
    }

    #[test]
    fn missing_and_unknown_requests_error() {
        let s = sources();
        assert!(s.payload_for("TZSW", Generation::Split).is_err());
        assert!(s.payload_for("WHAT", Generation::Split).is_err());
    }

    #[test]
    fn remap_and_loose_files_win() {
        let mut s = sources();
        s.remap.insert("WHAT".into(), "gsa_bl1".into());
        assert_eq!(
            s.payload_for("WHAT", Generation::Split).unwrap().0,
            vec![7u8; 10]
        );
        s.files.insert("bl1".into(), vec![5u8; 3]);
        assert_eq!(
            s.payload_for("BL1", Generation::Split).unwrap().0,
            vec![5u8; 3]
        );
    }

    #[test]
    fn loose_pack_uses_tensor_usbdl_names() {
        let mut s = Sources {
            pack: None,
            files: HashMap::new(),
            remap: HashMap::new(),
        };
        s.files.insert("gsa".into(), vec![1u8; 10]);
        s.files.insert("gsaf".into(), vec![2u8; 20]);
        assert_eq!(
            s.payload_for("GSA1", Generation::Split).unwrap().0,
            vec![1u8; 10]
        );
        assert_eq!(
            s.payload_for("GSAF", Generation::Split).unwrap().0,
            vec![2u8; 20]
        );
        assert!(s.payload_for("ABL", Generation::Split).is_err());
    }

    fn tagged(tag: &[u8; 4], fill: u8, body: usize) -> Vec<u8> {
        let mut img = vec![fill; HEADER_LEN + body];
        img[0x400..0x404].copy_from_slice(tag);
        img[0x40c..0x410].copy_from_slice(&(body as u32).to_le_bytes());
        img[0x410..0x414].copy_from_slice(&0x211u32.to_le_bytes());
        img
    }

    #[test]
    fn header_tags_beat_file_names() {
        // tensor-usbdl's husky pack: gsa.img carries the GSAF image and
        // gsaf.img the GSA1 image, so names alone would send them swapped.
        let mut s = Sources {
            pack: None,
            files: HashMap::new(),
            remap: HashMap::new(),
        };
        s.files.insert("gsa".into(), tagged(b"GSAF", 1, 100));
        s.files.insert("gsaf".into(), tagged(b"GSA1", 2, 50));
        s.files.insert("bl1".into(), tagged(b"APBL", 3, 10));
        s.files.insert("tzsw".into(), tagged(b"TZSW", 4, 7));
        let (gsa1, desc) = s.payload_for("GSA1", Generation::Split).unwrap();
        assert_eq!(gsa1.len(), HEADER_LEN + 50);
        assert_eq!(gsa1[0], 2);
        assert!(desc.contains("gsaf.img"), "{desc}");
        assert_eq!(s.payload_for("GSAF", Generation::Split).unwrap().0[0], 1);
        assert_eq!(s.payload_for("BL1", Generation::Split).unwrap().0[0], 3);
        assert_eq!(
            s.payload_for("TZSB", Generation::Split).unwrap().0,
            vec![4u8; 7]
        );
        assert_eq!(header_tag(&tagged(b"BL2\0", 0, 1)).as_deref(), Some("BL2"));
        assert_eq!(header_tag(&[0xffu8; HEADER_LEN]), None);
    }
}
