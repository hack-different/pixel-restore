//! Pick the oldest factory image a phone's bootloader anti-rollback level
//! still accepts. Flashing below that level leaves a Tensor Pixel in ROM
//! recovery, and for models with no public USB-bootable BL1 (Pixel 6, 9, 10
//! families) nothing brings it back, so every unknown here is an error.
//!
//! The level is not readable over fastboot and its header field is not
//! documented, so it is inferred from what Google publishes:
//! - the phone's level is that of the bootloader it is running
//!   (`getvar version-bootloader`), found by name in a listed factory image;
//! - the listing page warns when a bootloader "increments the anti-roll back
//!   version", and [`BUMPS`] records every such month per model.
//!
//! A bump Google never announced is the one thing this cannot see.

use crate::fetch::Image;
use anyhow::{bail, Result};
use std::collections::HashMap;

/// (year, month)
pub type Month = (u32, u32);

/// Months a bootloader update raised the anti-rollback version, per model,
/// as announced on <https://developers.google.com/android/images>.
pub const BUMPS: &[(&str, &[Month])] = &[
    ("oriole", &[(2022, 8), (2025, 5)]),
    ("raven", &[(2022, 8), (2025, 5)]),
    ("bluejay", &[(2022, 8), (2025, 5)]),
    ("shiba", &[(2025, 5)]),
    ("husky", &[(2025, 5)]),
    ("akita", &[(2025, 5)]),
    ("frankel", &[(2026, 5)]),
    ("blazer", &[(2026, 5)]),
    ("mustang", &[(2026, 5)]),
    ("rango", &[(2026, 5)]),
];

/// The family name Google's warnings use ("Pixel 9", "Pixel Fold").
const FAMILIES: &[(&str, &str)] = &[
    ("oriole", "6"),
    ("raven", "6"),
    ("bluejay", "6"),
    ("panther", "7"),
    ("cheetah", "7"),
    ("lynx", "7"),
    ("felix", "Fold"),
    ("tangorpro", "Tablet"),
    ("shiba", "8"),
    ("husky", "8"),
    ("akita", "8"),
    ("tokay", "9"),
    ("caiman", "9"),
    ("komodo", "9"),
    ("comet", "9"),
    ("tegu", "9"),
    ("frankel", "10"),
    ("blazer", "10"),
    ("mustang", "10"),
    ("rango", "10"),
];

const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];

const CARRIERS: &[&str] = &[
    "verizon", "t-mobile", "at&t", "telstra", "softbank", "kddi", "docomo", "emea", "jp", "tw",
    "rakuten", "carrier",
];

#[derive(Debug, Clone)]
pub struct Choice {
    pub image: Image,
    /// The bump month nothing older may go below, e.g. "May 2025".
    pub floor: Option<String>,
    /// The listed build whose bootloader the phone is running.
    pub phone_build: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    Safe,
    Refused(String),
}

pub fn month_name((y, m): Month) -> String {
    let n = MONTHS[(m - 1) as usize];
    format!("{}{} {y}", n[..1].to_ascii_uppercase(), &n[1..])
}

/// Every "May 2025"-style date in `text`, in order.
fn months_in(text: &str) -> Vec<Month> {
    let words: Vec<String> = text
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|w| !w.is_empty())
        .map(|w| w.to_ascii_lowercase())
        .collect();
    let mut found = Vec::new();
    for pair in words.windows(2) {
        let (w, y) = (&pair[0], &pair[1]);
        let Some(m) = MONTHS.iter().position(|m| w.starts_with(m)) else {
            continue;
        };
        // "may" or "sep"/"sept"/"september", not "mayor" or "separate".
        let full = [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ][m];
        if !(full.starts_with(w.as_str()) || w == "sept") {
            continue;
        }
        if y.len() == 4 && y.starts_with("20") {
            if let Ok(year) = y.parse() {
                found.push((year, m as u32 + 1));
            }
        }
    }
    found
}

/// The month in a listing row's description, e.g. "16.0.0 (BP1A..., May 2025)".
pub fn image_month(image: &Image) -> Option<Month> {
    months_in(&image.description).into_iter().next()
}

fn is_carrier(image: &Image) -> bool {
    let d = image.description.to_ascii_lowercase();
    CARRIERS
        .iter()
        .any(|c| d.contains(&format!("({c}")) || d.contains(&format!(", {c}")))
}

fn strip_tags(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_tag = false;
    for c in s.chars() {
        match c {
            '<' => in_tag = true,
            '>' => {
                in_tag = false;
                out.push(' ');
            }
            _ if !in_tag => out.push(c),
            _ => {}
        }
    }
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Families ("6", "Fold") named as "Pixel X" in `text`.
fn families_in(text: &str) -> Vec<String> {
    let words: Vec<&str> = text
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|w| !w.is_empty())
        .collect();
    words
        .windows(2)
        .filter(|p| p[0].eq_ignore_ascii_case("pixel"))
        .filter_map(|p| {
            let w = p[1];
            let digits: String = w.chars().take_while(|c| c.is_ascii_digit()).collect();
            if !digits.is_empty() {
                Some(digits) // "6a" is family 6
            } else if w == "Fold" || w == "Tablet" {
                Some(w.to_string())
            } else {
                None
            }
        })
        .collect()
}

/// (families named, months named) for each block of the listing page that
/// mentions the anti-rollback version.
pub fn warnings(page_html: &str) -> Vec<(Vec<String>, Vec<Month>)> {
    let lower = page_html.to_ascii_lowercase();
    let mut cuts = vec![0];
    for end in ["</p>", "</li>", "</aside>", "</div>", "<br", "</tr>", "</h"] {
        cuts.extend(lower.match_indices(end).map(|(i, _)| i));
    }
    cuts.push(page_html.len());
    cuts.sort_unstable();
    cuts.dedup();
    cuts.windows(2)
        .filter_map(|w| {
            let text = strip_tags(&page_html[w[0]..w[1]]);
            let t = text.to_ascii_lowercase();
            let hit = ["anti-roll", "anti roll", "antiroll"]
                .iter()
                .any(|k| t.contains(k));
            hit.then(|| (families_in(&text), months_in(&text)))
        })
        .collect()
}

fn bumps(product: &str) -> &'static [Month] {
    BUMPS
        .iter()
        .find(|(p, _)| *p == product)
        .map(|(_, b)| *b)
        .unwrap_or(&[])
}

/// Fail when the page announces a bump for this family that [`BUMPS`] lacks,
/// or carries no anti-rollback warning at all (its layout changed).
pub fn check_page(product: &str, page_html: &str) -> Result<()> {
    let Some((_, family)) = FAMILIES.iter().find(|(p, _)| *p == product) else {
        bail!("{product} is not a known Tensor Pixel; cannot judge its rollback level");
    };
    let found = warnings(page_html);
    if found.is_empty() {
        bail!(
            "the factory image page has no anti-rollback warnings at all; its layout has \
             changed, so unannounced bumps cannot be ruled out"
        );
    }
    let known = bumps(product);
    for (families, months) in found {
        if !families.iter().any(|f| f == family) {
            continue;
        }
        let mut new: Vec<Month> = months.into_iter().filter(|m| !known.contains(m)).collect();
        new.sort_unstable();
        new.dedup();
        if !new.is_empty() {
            bail!(
                "the factory image page announces an anti-rollback bump for Pixel {family} in {} \
                 that this tool does not know; add it to arb::BUMPS before downgrading",
                new.iter()
                    .map(|m| month_name(*m))
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }
    }
    Ok(())
}

struct Floor {
    phone_month: Month,
    phone_build: String,
    floor: Option<Month>,
}

/// `bootloaders` maps a listed build id to the version in its
/// `bootloader-<product>-<version>.img`; only the phone's build is required.
fn floor(
    product: &str,
    phone_bootloader: &str,
    images: &[Image],
    bootloaders: &HashMap<String, String>,
    page_html: &str,
) -> Result<Floor> {
    check_page(product, page_html)?;
    if phone_bootloader.trim().is_empty() {
        bail!("the phone did not report version-bootloader");
    }
    let running = images.iter().filter(|i| {
        bootloaders
            .get(&i.build.to_ascii_lowercase())
            .or_else(|| bootloaders.get(&i.build))
            .is_some_and(|v| v.eq_ignore_ascii_case(phone_bootloader.trim()))
    });
    let mut best: Option<(Month, &Image)> = None;
    for image in running {
        let Some(m) = image_month(image) else {
            bail!(
                "cannot read the month of {} ({})",
                image.build,
                image.description
            );
        };
        if best.is_none_or(|(b, _)| m < b) {
            best = Some((m, image));
        }
    }
    let Some((phone_month, phone_image)) = best else {
        bail!(
            "no listed factory image carries bootloader {phone_bootloader} (a beta or OTA-only \
             build?), so its rollback level is unknown; refusing to downgrade"
        );
    };
    let floor = bumps(product)
        .iter()
        .copied()
        .filter(|b| *b <= phone_month)
        .max();
    Ok(Floor {
        phone_month,
        phone_build: phone_image.build.clone(),
        floor,
    })
}

fn allowed(month: Option<Month>, floor: Option<Month>) -> bool {
    match (month, floor) {
        (None, _) => false,
        (Some(_), None) => true,
        (Some(m), Some(f)) => m >= f,
    }
}

/// Is `candidate_build` at or above the phone's rollback floor? `Err` means
/// the floor itself cannot be established: treat it as unknown, not safe.
pub fn verdict(
    product: &str,
    phone_bootloader: &str,
    images: &[Image],
    bootloaders: &HashMap<String, String>,
    page_html: &str,
    candidate_build: &str,
) -> Result<Verdict> {
    let f = floor(product, phone_bootloader, images, bootloaders, page_html)?;
    let Some(image) = images
        .iter()
        .find(|i| i.build.eq_ignore_ascii_case(candidate_build))
    else {
        bail!("no build {candidate_build} for {product} on the factory image page");
    };
    let month = image_month(image);
    Ok(if allowed(month, f.floor) {
        Verdict::Safe
    } else if month.is_none() {
        Verdict::Refused(format!(
            "cannot read the month of {candidate_build} ({})",
            image.description
        ))
    } else {
        Verdict::Refused(format!(
            "{candidate_build} ({}) is older than the phone's rollback floor {}; flashing it \
             would brick the phone",
            image.description,
            month_name(f.floor.unwrap())
        ))
    })
}

/// The oldest generic image at or above the floor, or `build` if given and
/// allowed.
pub fn lowest_safe(
    product: &str,
    phone_bootloader: &str,
    images: &[Image],
    bootloaders: &HashMap<String, String>,
    page_html: &str,
    build: Option<&str>,
) -> Result<Choice> {
    let f = floor(product, phone_bootloader, images, bootloaders, page_html)?;
    let image = if let Some(b) = build {
        match verdict(product, phone_bootloader, images, bootloaders, page_html, b)? {
            Verdict::Refused(why) => bail!(why),
            Verdict::Safe => images
                .iter()
                .find(|i| i.build.eq_ignore_ascii_case(b))
                .unwrap()
                .clone(),
        }
    } else {
        images
            .iter()
            .enumerate()
            .filter(|(_, i)| !is_carrier(i))
            .filter_map(|(n, i)| image_month(i).map(|m| (m, n, i)))
            .filter(|(m, _, _)| allowed(Some(*m), f.floor))
            .min_by_key(|(m, n, _)| (*m, *n))
            .map(|(_, _, i)| i.clone())
            .ok_or_else(|| {
                anyhow::anyhow!("no generic factory image at or above the phone's rollback floor")
            })?
    };
    let reason = match f.floor {
        Some(fl) => format!(
            "phone runs bootloader {phone_bootloader} ({}), past the {} anti-rollback bump, so \
             nothing older than {} boots",
            month_name(f.phone_month),
            month_name(fl),
            month_name(fl)
        ),
        None => format!(
            "no published anti-rollback bump for {product} at or before {}, so the oldest \
             listed build qualifies",
            month_name(f.phone_month)
        ),
    };
    Ok(Choice {
        image,
        floor: f.floor.map(month_name),
        phone_build: f.phone_build,
        reason,
    })
}

/// Cross-check with the bootloader's own counters (`getvar ap-ar-ns`,
/// `ap-ar-s`, `gsa-bl1-ar`, `gsa-fw-ar`): `candidate` is what a phone reported
/// while running the candidate bootloader (from `anti-rollback.tsv`). Any
/// counter the phone now holds above that means the candidate is below its
/// level, whatever the announced months say. A recorded value can only
/// understate a bootloader's level, so this errs towards refusing.
pub fn counters_verdict(phone: &[(&str, String)], candidate: &[(&str, String)]) -> Verdict {
    let num = |v: &str| v.trim().parse::<u64>().ok();
    let below: Vec<String> = phone
        .iter()
        .filter_map(|(name, have)| {
            let (_, was) = candidate.iter().find(|(n, _)| n == name)?;
            match (num(have), num(was)) {
                (Some(h), Some(w)) if w < h => Some(format!("{name} {w} < {h}")),
                (Some(_), Some(_)) => None,
                _ => Some(format!("{name} unreadable ({was} vs {have})")),
            }
        })
        .collect();
    if below.is_empty() {
        Verdict::Safe
    } else {
        Verdict::Refused(format!(
            "the phone's anti-rollback counters are above the candidate bootloader's: {}; \
             flashing it would brick the phone",
            below.join(", ")
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PAGE: &str = r#"
<aside class="warning"><p><strong>Warning:</strong> The May 2025 update for Pixel 6,
Pixel 6 Pro, Pixel 6a, Pixel 8, Pixel 8 Pro and Pixel 8a contains a bootloader update
that increments the anti-roll back version for the bootloader.</p></aside>
<p>The inactive slot keeps an older bootloader whose anti-rollback version has not
been incremented.</p>
<aside class="warning"><p>The May 2026 update for Pixel 10, Pixel 10 Pro, Pixel 10 Pro XL
and Pixel 10 Pro Fold increments the anti-roll back version.</p></aside>
"#;

    fn rows(device: &str, specs: &[(&str, &str, &str)]) -> (Vec<Image>, HashMap<String, String>) {
        let images = specs
            .iter()
            .map(|(b, month, _)| Image {
                device: device.into(),
                build: b.to_string(),
                url: format!("u/{b}"),
                sha256: None,
                description: format!("16.0.0 ({}, {month})", b.to_ascii_uppercase()),
            })
            .collect();
        let bls = specs
            .iter()
            .map(|(b, _, bl)| (b.to_string(), bl.to_string()))
            .collect();
        (images, bls)
    }

    fn husky() -> (Vec<Image>, HashMap<String, String>) {
        rows(
            "husky",
            &[
                ("ud1a.231105.004", "Nov 2023", "ripcurrent-14.0-1"),
                ("ap1a.240505.004", "Jul 2024", "ripcurrent-14.5-2"),
                ("bp1a.250405.007", "Apr 2025", "ripcurrent-15.1-3"),
                ("bp1a.250505.005", "May 2025", "ripcurrent-15.2-4"),
                (
                    "bp1a.250505.005.v1",
                    "May 2025, Verizon",
                    "ripcurrent-15.2-4",
                ),
                ("bp1a.250605.002", "Jun 2025", "ripcurrent-15.3-5"),
                ("cp3a.260905.009", "Sep 2026", "ripcurrent-17.0-6"),
            ],
        )
    }

    #[test]
    fn past_bump_floor_is_first_bumped_build() {
        let (i, b) = husky();
        let c = lowest_safe("husky", "ripcurrent-17.0-6", &i, &b, PAGE, None).unwrap();
        assert_eq!(c.image.build, "bp1a.250505.005");
        assert_eq!(c.floor.as_deref(), Some("May 2025"));
        assert_eq!(c.phone_build, "cp3a.260905.009");
    }

    #[test]
    fn before_bump_gets_oldest_listed_build() {
        let (i, b) = husky();
        let c = lowest_safe("husky", "RIPCURRENT-14.5-2", &i, &b, PAGE, None).unwrap();
        assert_eq!(c.image.build, "ud1a.231105.004");
        assert!(c.floor.is_none());
    }

    #[test]
    fn verdicts_and_explicit_builds() {
        let (i, b) = husky();
        let v = |c| verdict("husky", "ripcurrent-17.0-6", &i, &b, PAGE, c).unwrap();
        assert!(matches!(v("BP1A.250405.007"), Verdict::Refused(w) if w.contains("brick")));
        assert_eq!(v("bp1a.250605.002"), Verdict::Safe);
        assert_eq!(v("bp1a.250505.005"), Verdict::Safe);
        let e = lowest_safe(
            "husky",
            "ripcurrent-17.0-6",
            &i,
            &b,
            PAGE,
            Some("bp1a.250405.007"),
        );
        assert!(e.unwrap_err().to_string().contains("brick"));
        assert!(verdict("husky", "ripcurrent-17.0-6", &i, &b, PAGE, "nope").is_err());
    }

    #[test]
    fn unknown_phone_bootloader_is_an_error() {
        let (i, b) = husky();
        let e = lowest_safe("husky", "ripcurrent-beta-9", &i, &b, PAGE, None).unwrap_err();
        assert!(e.to_string().contains("rollback level is unknown"));
        assert!(lowest_safe("husky", " ", &i, &b, PAGE, None).is_err());
    }

    #[test]
    fn pixel6_takes_latest_of_two_bumps() {
        let (i, b) = rows(
            "oriole",
            &[
                ("sd1a", "Oct 2021", "slider-1"),
                ("tp1a", "Aug 2022", "slider-2"),
                ("bp1a", "May 2025", "slider-3"),
                ("cp1a", "Jun 2026", "slider-4"),
            ],
        );
        let pick = |bl| {
            lowest_safe("oriole", bl, &i, &b, PAGE, None)
                .unwrap()
                .image
                .build
        };
        assert_eq!(pick("slider-2"), "tp1a");
        assert_eq!(pick("slider-4"), "bp1a");
    }

    #[test]
    fn model_without_bumps_gets_launch_build() {
        let (i, b) = rows(
            "komodo",
            &[
                ("ad1a.240905.004", "Sep 2024", "ripcurrentpro-1"),
                ("cp3a.260905.009", "Sep 2026", "ripcurrentpro-9"),
            ],
        );
        let c = lowest_safe("komodo", "ripcurrentpro-9", &i, &b, PAGE, None).unwrap();
        assert_eq!(c.image.build, "ad1a.240905.004");
    }

    #[test]
    fn unannounced_bump_fails_closed_for_that_family_only() {
        let (i, b) = rows(
            "komodo",
            &[("a", "Sep 2024", "x-1"), ("b", "Sep 2026", "x-2")],
        );
        let p9 = format!(
            "{PAGE}<p>The March 2027 update for Pixel 9 increments the anti-roll back version.</p>"
        );
        let e = lowest_safe("komodo", "x-2", &i, &b, &p9, None).unwrap_err();
        assert!(e.to_string().contains("Mar 2027"), "{e}");
        let p7 = format!(
            "{PAGE}<p>The March 2027 update for Pixel 7 increments the anti-roll back version.</p>"
        );
        assert_eq!(
            lowest_safe("komodo", "x-2", &i, &b, &p7, None)
                .unwrap()
                .image
                .build,
            "a"
        );
        // "Pixel 10" must not read as Pixel 1.
        assert_eq!(warnings(PAGE)[2].0, vec!["10"; 4]);
    }

    #[test]
    fn page_without_warnings_or_unknown_model_fails_closed() {
        let (i, b) = husky();
        let e = lowest_safe("husky", "ripcurrent-17.0-6", &i, &b, "<p>nothing</p>", None);
        assert!(e.unwrap_err().to_string().contains("layout"));
        let e = lowest_safe("sunfish", "x", &i, &b, PAGE, None);
        assert!(e.unwrap_err().to_string().contains("not a known Tensor"));
    }

    #[test]
    fn months_parse_strictly() {
        assert_eq!(months_in("16.0.0 (BP1A, May 2025)"), vec![(2025, 5)]);
        assert_eq!(
            months_in("September 2026 and Sept 2025"),
            vec![(2026, 9), (2025, 9)]
        );
        assert!(months_in("mayor 2025, separate 2026").is_empty());
    }

    #[test]
    fn counters_refuse_a_lower_candidate() {
        let c = |a: &str, b: &str| vec![("ap-ar-ns", a.to_string()), ("gsa-bl1-ar", b.to_string())];
        assert_eq!(counters_verdict(&c("3", "2"), &c("3", "2")), Verdict::Safe);
        assert_eq!(counters_verdict(&c("3", "2"), &c("4", "2")), Verdict::Safe);
        assert!(matches!(counters_verdict(&c("3", "2"), &c("2", "2")),
            Verdict::Refused(w) if w.contains("ap-ar-ns 2 < 3")));
        assert!(matches!(
            counters_verdict(&c("3", "x"), &c("3", "2")),
            Verdict::Refused(_)
        ));
        // A counter only one side reports proves nothing either way.
        assert_eq!(
            counters_verdict(&c("3", "2")[..1], &c("3", "2")[1..]),
            Verdict::Safe
        );
    }
}
