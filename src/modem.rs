//! Embed the standard-library-only Python helper so installed binaries can use it.
use anyhow::{bail, Context, Result};
use std::process::Command;

pub fn run(args: &[String]) -> Result<()> {
    let python = std::env::var_os("PIXEL_RESTORE_PYTHON").unwrap_or_else(|| "python3".into());
    let status = Command::new(python)
        .arg("-c")
        .arg(include_str!("../tools/pixel_modem.py"))
        .args(args)
        .status()
        .context("starting modem helper; install Python 3.9+ or set PIXEL_RESTORE_PYTHON")?;
    if !status.success() {
        bail!("modem helper exited with {status}");
    }
    Ok(())
}
