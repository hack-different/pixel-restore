//! The original restore_pixel_tensor workflow supplies one complete .bin
//! payload per ROM request. Preserve those bytes, including pre-split bodies.
use anyhow::{bail, Context, Result};
use std::collections::HashMap;
use std::path::Path;

pub fn load(dir: &Path) -> Result<HashMap<String, Vec<u8>>> {
    let mut paths = std::fs::read_dir(dir)
        .with_context(|| format!("reading firmware directory {}", dir.display()))?
        .map(|entry| entry.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    paths.sort();
    let mut files = HashMap::new();
    for path in paths {
        if !path.is_file()
            || !path
                .extension()
                .and_then(|e| e.to_str())
                .is_some_and(|e| e.eq_ignore_ascii_case("bin"))
        {
            continue;
        }
        let name = path.file_stem().and_then(|n| n.to_str()).unwrap_or("");
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
            bail!(
                "invalid request filename {}; expected STAGE.bin",
                path.display()
            );
        }
        let stage = name.to_ascii_uppercase();
        if files.contains_key(&stage) {
            bail!("multiple .bin files name the same ROM request {stage}");
        }
        let bytes = std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
        if bytes.is_empty() {
            bail!("empty request payload {}", path.display());
        }
        files.insert(stage, bytes);
    }
    if files.is_empty() {
        bail!("{} holds no STAGE.bin request payloads", dir.display());
    }
    Ok(files)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    struct Directory(std::path::PathBuf);
    impl Directory {
        fn new() -> Self {
            let dir = std::env::temp_dir().join(format!(
                "pixel-firmware-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&dir).unwrap();
            Self(dir)
        }
        fn put(&self, name: &str, bytes: &[u8]) {
            std::fs::write(self.0.join(name), bytes).unwrap();
        }
    }
    impl Drop for Directory {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }
    #[test]
    fn loads_case_insensitive_request_names_and_ignores_non_payloads() {
        let dir = Directory::new();
        dir.put("bl1.bin", &[0, 255]);
        dir.put("ABLB.BIN", &[4, 5, 6]);
        dir.put("notes.txt", b"not firmware");
        std::fs::create_dir(dir.0.join("ignored.bin")).unwrap();
        let files = load(&dir.0).unwrap();
        assert_eq!(files.len(), 2);
        assert_eq!(files["BL1"], [0, 255]);
        assert_eq!(files["ABLB"], [4, 5, 6]);
    }
    #[test]
    fn rejects_missing_empty_and_invalid_inputs() {
        let dir = Directory::new();
        assert!(load(&dir.0.join("missing")).is_err());
        assert!(load(&dir.0).is_err());
        dir.put("ABL.bin", b"");
        assert!(load(&dir.0).unwrap_err().to_string().contains("empty"));
        dir.put("ABL.bin", &[1]);
        dir.put("bad-name.bin", &[2]);
        assert!(load(&dir.0).unwrap_err().to_string().contains("invalid"));
    }
}
