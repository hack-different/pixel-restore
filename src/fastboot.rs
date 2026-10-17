//! A thin wrapper around the platform `fastboot` binary, for the steps that
//! happen once the phone is out of ROM Recovery. Every call names the device
//! with `-s`, but the serial is picked automatically when only one phone is
//! in fastboot, so it only has to be given when several are attached.

use anyhow::{bail, ensure, Context, Result};
use std::path::Path;
use std::process::{Command, Output};

/// The binary to run: `$FASTBOOT` if set, else `fastboot` from PATH.
fn binary() -> String {
    std::env::var("FASTBOOT").unwrap_or_else(|_| "fastboot".to_string())
}

fn run(args: &[&str]) -> Result<Output> {
    let bin = binary();
    Command::new(&bin)
        .args(args)
        .output()
        .with_context(|| format!("running `{bin}`; is platform-tools installed and on PATH?"))
}

/// Serials from `fastboot devices` output (`<serial>\tfastboot` per line).
pub fn parse_devices(out: &str) -> Vec<String> {
    out.lines()
        .filter_map(|l| {
            let mut cols = l.split_whitespace();
            let serial = cols.next()?;
            // Phones in fastbootd say "fastbootd"; both take flash commands.
            matches!(cols.next(), Some("fastboot" | "fastbootd")).then(|| serial.to_string())
        })
        .collect()
}

/// Serials of every phone in fastboot right now.
pub fn devices() -> Result<Vec<String>> {
    let out = run(&["devices"])?;
    Ok(parse_devices(&String::from_utf8_lossy(&out.stdout)))
}

/// Choose which phone to talk to: the one asked for, or the only one attached.
pub fn choose(wanted: Option<&str>, attached: &[String]) -> Result<String> {
    match (wanted, attached) {
        (Some(s), _) if attached.iter().any(|a| a == s) => Ok(s.to_string()),
        (Some(s), []) => bail!("{s} is not in fastboot (no fastboot devices attached)"),
        (Some(s), _) => bail!("{s} is not in fastboot; attached: {}", attached.join(", ")),
        (None, [only]) => Ok(only.clone()),
        (None, []) => bail!(
            "no phone in fastboot. Hold Volume Down while powering on, or run \
             `adb reboot bootloader`, then check `fastboot devices`"
        ),
        (None, _) => bail!(
            "{} phones are in fastboot ({}); pick one with --serial",
            attached.len(),
            attached.join(", ")
        ),
    }
}

/// The value of `name` from `fastboot getvar` output, which goes to stderr
/// as `<name>: <value>` (or `(bootloader) <name>: <value>` on older tools).
pub fn parse_getvar(out: &str, name: &str) -> Option<String> {
    out.lines().find_map(|l| {
        let l = l.trim().trim_start_matches("(bootloader)").trim();
        let v = l.strip_prefix(name)?.strip_prefix(':')?.trim();
        (!v.is_empty()).then(|| v.to_string())
    })
}

/// One phone in fastboot.
pub struct Device {
    pub serial: String,
}

impl Device {
    /// Open the phone with this serial, or the only phone in fastboot.
    pub fn open(serial: Option<&str>) -> Result<Device> {
        let serial = choose(serial, &devices()?)?;
        Ok(Device { serial })
    }

    pub fn getvar(&self, name: &str) -> Result<Option<String>> {
        let out = run(&["-s", &self.serial, "getvar", name])?;
        let text = String::from_utf8_lossy(&out.stderr).into_owned()
            + &String::from_utf8_lossy(&out.stdout);
        Ok(parse_getvar(&text, name))
    }

    fn checked(&self, args: &[&str]) -> Result<()> {
        let mut full = vec!["-s", self.serial.as_str()];
        full.extend_from_slice(args);
        let out = run(&full)?;
        let stderr = String::from_utf8_lossy(&out.stderr);
        ensure!(
            out.status.success(),
            "`fastboot {}` failed:\n{}",
            args.join(" "),
            stderr.trim()
        );
        Ok(())
    }

    pub fn flash(&self, partition: &str, image: &Path) -> Result<()> {
        let path = image.to_string_lossy();
        self.checked(&["flash", partition, &path])
    }

    /// `fastboot oem <args>`: whether it succeeded, and everything it printed
    /// (the bootloader's `(bootloader) ...` lines go to stderr).
    pub fn oem(&self, args: &[&str]) -> Result<(bool, String)> {
        let mut full = vec!["-s", self.serial.as_str(), "oem"];
        full.extend_from_slice(args);
        let out = run(&full)?;
        let text = String::from_utf8_lossy(&out.stderr).into_owned()
            + &String::from_utf8_lossy(&out.stdout);
        Ok((out.status.success() && !text.contains("FAILED"), text))
    }

    /// `fastboot stage <file>`: download it for the next command to use.
    pub fn stage(&self, file: &Path) -> Result<()> {
        let path = file.to_string_lossy();
        self.checked(&["stage", &path])
    }

    pub fn reboot_bootloader(&self) -> Result<()> {
        self.checked(&["reboot-bootloader"])
    }

    pub fn reboot(&self) -> Result<()> {
        self.checked(&["reboot"])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_device_list() {
        let out = "45171FDJG0010W\tfastboot\n44260DLJH000Q3\tfastbootd\n\n";
        assert_eq!(parse_devices(out), ["45171FDJG0010W", "44260DLJH000Q3"]);
        assert!(parse_devices("").is_empty());
        // adb-style or junk lines are not fastboot devices.
        assert!(parse_devices("List of devices attached\nX\tdevice\n").is_empty());
    }

    #[test]
    fn serial_is_optional_with_one_phone() {
        let one = vec!["A".to_string()];
        let two = vec!["A".to_string(), "B".to_string()];
        assert_eq!(choose(None, &one).unwrap(), "A");
        assert_eq!(choose(Some("B"), &two).unwrap(), "B");
        assert!(choose(None, &two)
            .unwrap_err()
            .to_string()
            .contains("--serial"));
        assert!(choose(None, &[]).is_err());
        assert!(choose(Some("C"), &two).is_err());
    }

    #[test]
    fn parses_getvar_output() {
        let out = "product: komodo\nFinished. Total time: 0.001s\n";
        assert_eq!(parse_getvar(out, "product").as_deref(), Some("komodo"));
        let old = "(bootloader) version-baseband: g5400c-250909-250916-B-14110587\nOKAY\n";
        assert_eq!(
            parse_getvar(old, "version-baseband").as_deref(),
            Some("g5400c-250909-250916-B-14110587")
        );
        // A name that is a prefix of another variable must not match it.
        assert_eq!(parse_getvar("version-bootloader: x\n", "version"), None);
        assert_eq!(parse_getvar("unlocked: \n", "unlocked"), None);
    }
}
