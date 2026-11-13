//! Pairing with the daemon: the owner asks for a code (POST /pair/start, no credential),
//! reads the 6 digits on the Windows desktop, types them in the widget (POST /pair/finish).
//! The daemon returns a widget token once; it goes straight to the Windows vault.
//! No shell, no WSL command and no token command are involved any more.

pub const CLIENT_NAME: &str = "Omnigroup (Windows)";
const CODE_DIGITS: usize = 6;

/// Six ASCII digits (spaces around are tolerated), or a French error.
pub fn validate_code(raw: &str) -> Result<String, String> {
    let code = raw.trim();
    if code.len() != CODE_DIGITS || !code.chars().all(|c| c.is_ascii_digit()) {
        return Err("Le code de couplage fait 6 chiffres.".into());
    }
    Ok(code.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_are_six_ascii_digits() {
        assert_eq!(validate_code(" 042137 ").unwrap(), "042137");
        assert!(validate_code("42137").is_err());
        assert!(validate_code("0421378").is_err());
        assert!(validate_code("04213a").is_err());
        assert!(validate_code("０４２１３７").is_err()); // full-width digits
    }
}
