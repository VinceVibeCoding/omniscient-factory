//! Daemon proof (M5): before sending its token, the widget checks that the daemon on the
//! loopback port is the one it paired with. GET /health?nonce=N must list
//! HMAC-SHA256(key = SHA-256(widget token), message = N), hex. The daemon keeps only the
//! token's SHA-256, so it can compute this; an impostor on the port cannot.

use hmac::{Hmac, Mac};
use serde_json::Value;
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;
const NONCE_BYTES: usize = 16;

/// Lowercase hex of bytes.
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// 16 random bytes from the OS, hex (32 URL-safe characters).
pub fn new_nonce() -> Result<String, String> {
    let mut buf = [0u8; NONCE_BYTES];
    getrandom::getrandom(&mut buf).map_err(|err| format!("Aléa indisponible : {err}"))?;
    Ok(hex(&buf))
}

/// The proof a genuine daemon returns for this token and nonce.
pub fn expected_proof(token: &str, nonce: &str) -> String {
    let key = Sha256::digest(token.as_bytes());
    let mut mac = HmacSha256::new_from_slice(key.as_slice()).expect("HMAC takes keys of any size");
    mac.update(nonce.as_bytes());
    let code = mac.finalize().into_bytes();
    hex(code.as_slice())
}

/// Equality that does not stop at the first different byte.
pub fn constant_time_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// True when one of the daemon's proofs matches ours.
pub fn proves(proofs: &Value, token: &str, nonce: &str) -> bool {
    let expected = expected_proof(token, nonce);
    match proofs.as_array() {
        Some(list) => list
            .iter()
            .filter_map(Value::as_str)
            .any(|proof| constant_time_eq(proof, &expected)),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const TOKEN: &str = "abcDEF0123456789_-xyz-widget-token";
    const NONCE: &str = "00112233445566778899aabbccddeeff";
    // Computed by the daemon side (omniscient.group.credentials.proof): both sides agree.
    const DAEMON_PROOF: &str = "fbbe1881b498da5847cd78833b9926d3d6402fec86a03fa16a84b45e47af523b";

    #[test]
    fn matches_the_python_daemon() {
        assert_eq!(expected_proof(TOKEN, NONCE), DAEMON_PROOF);
    }

    #[test]
    fn sha256_and_hex_are_standard() {
        let digest = Sha256::digest(b"abc");
        assert_eq!(hex(digest.as_slice()), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }

    #[test]
    fn accepts_only_a_matching_proof() {
        assert!(proves(&json!(["nope", DAEMON_PROOF]), TOKEN, NONCE));
        assert!(!proves(&json!(["nope"]), TOKEN, NONCE));
        assert!(!proves(&json!([DAEMON_PROOF]), "another-token-0123456789", NONCE));
        assert!(!proves(&json!(null), TOKEN, NONCE));
    }

    #[test]
    fn nonces_are_fresh_hex() {
        let (a, b) = (new_nonce().unwrap(), new_nonce().unwrap());
        assert_eq!(a.len(), 32);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn constant_time_eq_basics() {
        assert!(constant_time_eq("abc", "abc"));
        assert!(!constant_time_eq("abc", "abd"));
        assert!(!constant_time_eq("abc", "ab"));
    }
}
