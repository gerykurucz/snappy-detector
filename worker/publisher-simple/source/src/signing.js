/**
 * Signing and canonicalisation.
 *
 * tweetnacl rather than WebCrypto, deliberately. Cloudflare's WebCrypto exposes
 * Ed25519 as `NODE-ED25519` and refuses raw private-key import, while Node calls
 * the same algorithm `Ed25519` — so a WebCrypto implementation has to carry two
 * code paths and can only be tested on one of them. tweetnacl behaves identically
 * everywhere, which means the tests that run in Node are testing the code that
 * runs in the Worker.
 *
 * It is VENDORED (src/vendor/tweetnacl.js) rather than installed, because the npm
 * package is CommonJS and an ES-module Worker cannot import it.
 *
 * Interop was proven before this was written: a tweetnacl signature over the
 * canonical bytes verifies under the client's hand-rolled edsig.py, and
 * `edsig.public_from_secret(seed)` returns the same bytes tweetnacl derives.
 * Ed25519 is deterministic (RFC 8032), so any correct implementation produces
 * the same signature for the same seed and message.
 */

// Vendored, not installed from npm: tweetnacl is CommonJS-only, so an
// ES-module Worker cannot resolve it and the deploy fails at runtime with
// "No such module \"tweetnacl\"". See src/vendor/tweetnacl.js for the
// provenance, licence and the exact edits made to the UMD wrapper.
import nacl from "./vendor/tweetnacl.js";

/**
 * The canonical bytes that get signed.
 *
 * MUST match builder.sign_document() and online._canonical_bytes() exactly:
 * sorted keys, compact separators, `sig` excluded. A one-byte drift here does
 * not fail loudly — it locks out every customer at once, because every shipped
 * exe rejects the file.
 */
export function canonicalBytes(doc) {
  const payload = {};
  for (const key of Object.keys(doc).sort()) {
    if (key === "sig") continue;
    payload[key] = doc[key];
  }
  return new TextEncoder().encode(stableStringify(payload));
}

/** JSON.stringify with recursively sorted keys and no whitespace. */
function stableStringify(value) {
  if (Array.isArray(value)) {
    return "[" + value.map(stableStringify).join(",") + "]";
  }
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return (
      "{" +
      keys.map((k) => JSON.stringify(k) + ":" + stableStringify(value[k])).join(",") +
      "}"
    );
  }
  return JSON.stringify(value);
}

function parseSeed(secret) {
  const raw = String(secret || "").trim();
  if (!raw) throw new Error("ED25519_SECRET is not configured");

  let bytes;
  if (/^[0-9a-fA-F]{64}$/.test(raw)) {
    bytes = new Uint8Array(raw.match(/.{2}/g).map((h) => parseInt(h, 16)));
  } else {
    const decoded = Uint8Array.from(atob(raw), (c) => c.charCodeAt(0));
    if (decoded.length !== 32) {
      throw new Error(`Ed25519 seed must be 32 bytes, got ${decoded.length}`);
    }
    bytes = decoded;
  }
  return bytes;
}

export function keyPairFromSecret(secret) {
  return nacl.sign.keyPair.fromSeed(parseSeed(secret));
}

/** Base64 public key — the value every shipped client pins. */
export function publicKeyB64(secret) {
  return uint8ToB64(keyPairFromSecret(secret).publicKey);
}

/**
 * Sign a document and return a copy with `sig` attached.
 *
 * Fails loudly if the derived public key does not match the one the clients
 * pin. That is the same guard entitlements.yml had as EXPECTED_PUBKEY: without
 * it, rotating the secret without shipping the new public key is a silent total
 * lockout rather than a red deploy.
 */
export function signDocument(doc, secret, expectedPubkeyB64) {
  if (expectedPubkeyB64) {
    const actual = publicKeyB64(secret);
    if (actual !== String(expectedPubkeyB64).trim()) {
      throw new Error(
        `ED25519_SECRET does not derive to ED25519_EXPECTED_PUBKEY ` +
          `(expected ${expectedPubkeyB64}, derived ${actual}). Refusing to sign: ` +
          `every shipped client would reject the output.`
      );
    }
  }
  const sig = nacl.sign.detached(canonicalBytes(doc), keyPairFromSecret(secret).secretKey);
  return { ...doc, sig: uint8ToB64(sig) };
}

/** Verify a signed document. Mirrors what the client does. */
export function verifyDocument(doc, pubkeyB64) {
  try {
    return nacl.sign.detached.verify(
      canonicalBytes(doc),
      b64ToUint8(String(doc.sig || "")),
      b64ToUint8(pubkeyB64)
    );
  } catch {
    return false;
  }
}

export function uint8ToB64(bytes) {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

export function b64ToUint8(value) {
  return Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
}
