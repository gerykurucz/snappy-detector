/**
 * Stripe webhook signature verification.
 *
 * Identical scheme to the relay in ../stripe-relay/worker.js, which is already
 * deployed and tested: HMAC-SHA256 over `${t}.${rawBody}`, constant-time
 * compare, replay window enforced. Implemented from the spec with WebCrypto so
 * there is no dependency on the Stripe SDK inside a Worker.
 */

const TOLERANCE_SECONDS = 300;

export function parseSignatureHeader(header) {
  let timestamp = 0;
  const candidates = [];
  for (const part of String(header || "").split(",")) {
    const trimmed = part.trim();
    const eq = trimmed.indexOf("=");
    if (eq < 0) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (key === "t") timestamp = Number(value);
    else if (key === "v1" && value) candidates.push(value.toLowerCase());
  }
  if (!timestamp || candidates.length === 0) {
    throw new SignatureError("signature header missing t= or v1=");
  }
  return { timestamp, candidates };
}

export class SignatureError extends Error {
  /**
   * `status` is the HTTP code the caller should return. It defaults to 400
   * because almost every signature failure is the caller's fault and must not
   * be retried; 503 is used only for a server misconfiguration (no signing
   * secret), where 400 would wrongly imply the event itself was bad.
   *
   * This constructor was missing entirely at first, so `new SignatureError(msg,
   * 503)` silently produced an error with no status and the handler fell back
   * to 400 -- which is exactly the kind of bug a test asserting on the status
   * code exists to catch.
   */
  constructor(message, status = 400) {
    super(message);
    this.name = "SignatureError";
    this.status = status;
  }
}

/**
 * Verify against the EXACT bytes received. Re-serialising parsed JSON changes
 * whitespace and key order and every signature fails — the single most common
 * webhook bug, and it looks identical to a wrong secret.
 *
 * Multiple v1 candidates are accepted because Stripe publishes two signatures
 * while a signing secret is being rolled.
 *
 * The replay window does NOT need to cover Stripe's three-day retry schedule:
 * every retry is signed with a fresh timestamp. Double-application is prevented
 * separately, by the processed_events table.
 */
export async function verifySignature(rawBody, header, secret, nowSeconds) {
  if (!secret) {
    // 503-shaped: a configuration fault, not an event fault. Retrying cannot
    // fix it, so it must not be reported as a 5xx that Stripe retries for days.
    throw new SignatureError("relay has no signing secret configured", 503);
  }
  const { timestamp, candidates } = parseSignatureHeader(header);

  const age = Math.abs((nowSeconds ?? Date.now() / 1000) - timestamp);
  if (age > TOLERANCE_SECONDS) {
    throw new SignatureError("timestamp outside tolerance (possible replay)");
  }

  const expected = await hmacSha256Hex(
    secret,
    concat(new TextEncoder().encode(`${timestamp}.`), rawBody)
  );

  // Compare every candidate; do not short-circuit, so timing does not reveal
  // which position matched.
  let matched = false;
  for (const candidate of candidates) {
    if (fixedTimeEqual(expected, candidate)) matched = true;
  }
  if (!matched) throw new SignatureError("signature mismatch");
  return { timestamp, candidates };
}

async function hmacSha256Hex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, message);
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function concat(a, b) {
  const out = new Uint8Array(a.byteLength + b.byteLength);
  out.set(new Uint8Array(a), 0);
  out.set(new Uint8Array(b), a.byteLength);
  return out;
}

function fixedTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Test helper: build a valid Stripe-Signature header. */
export async function signHeader(rawBody, secret, timestamp) {
  const digest = await hmacSha256Hex(
    secret,
    concat(new TextEncoder().encode(`${timestamp}.`), rawBody)
  );
  return `t=${timestamp},v1=${digest}`;
}
