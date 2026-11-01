/**
 * SnappyAI entitlement publisher — no-database edition.
 *
 * Storage is ONE JSON file in your public repo. There is no D1, no KV, no
 * schema, and nothing to create in the Cloudflare dashboard besides the Worker
 * itself. See src/state-file.js for the file format and the trade-offs.
 *
 *   POST /webhooks/stripe     Stripe pushes; the row is applied and the file
 *                             republished in the same request (~300-600 ms)
 *   GET  /healthz             liveness + configuration state
 *   GET  /entitlements.json   the signed document, served directly
 *   GET  /admin/lookup        support: why is this customer locked out?
 *   POST /admin/grant         replaces `builder.py --grant`
 *   POST /admin/publish       force a republish
 *   scheduled()               hourly keepalive so generated_at never goes stale
 *
 * Secrets: STRIPE_WEBHOOK_SIGNING_SECRET, ED25519_SECRET, GITHUB_PAT, ADMIN_TOKEN
 * Vars:    PUBLIC_REPO, BRANCH, FILE_PATH, ED25519_EXPECTED_PUBKEY
 *          (ALL OPTIONAL — sane defaults for gerykurucz/snappyai-entitlements
 *          are baked in, so the dashboard single-file build needs zero vars)
 */

import {
  HANDLED_EVENT_TYPES,
  PRUNE_AFTER,
  applyEventToRow,
  checkRowFloor,
  extractEmail,
  isActive,
  rowKey,
  supportGrant,
} from "./entitlements.js";
import { publicKeyB64, signDocument, verifyDocument } from "./signing.js";
import { SignatureError, verifySignature } from "./stripe.js";
import {
  StateError,
  emptyState,
  hasSeen,
  markSeen,
  prune,
  readState,
  rememberIds,
  resolveHash,
  toPublicRows,
  writeState,
} from "./state-file.js";

const MAX_BODY_BYTES = 1_000_000;
const CAS_RETRIES = 4;

// ---------------------------------------------------------------------------
// Baked-in defaults. These are NOT secrets: the repo is public and the public
// key is already pinned inside every shipped client and the GitHub workflow
// (EXPECTED_PUBKEY). Baking them in means the single-file dashboard build
// (dashboard/worker.js) needs ZERO configuration vars — only the 4 secrets —
// because the Cloudflare dashboard upload flow ignores wrangler.toml entirely.
// Any of them can still be overridden by setting a var/secret in the dashboard.
// ---------------------------------------------------------------------------
const DEFAULT_PUBLIC_REPO = "gerykurucz/snappyai-entitlements";
const DEFAULT_BRANCH = "live";
const DEFAULT_FILE_PATH = "entitlements.json";
const DEFAULT_EXPECTED_PUBKEY = "J3Ulxeu/JPzIlGypJ28+h45rlw3aW6bJFfDu3luRGOE=";

function expectedPubkey(env) {
  return env.ED25519_EXPECTED_PUBKEY || DEFAULT_EXPECTED_PUBKEY;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cfg = repoConfig(env);

    try {
      if (request.method === "GET" && url.pathname === "/healthz") {
        return json(await health(env, cfg));
      }
      if (request.method === "POST" && url.pathname === "/webhooks/stripe") {
        return await handleStripe(request, env, cfg);
      }
      if (request.method === "GET" && url.pathname === "/entitlements.json") {
        return await serveDocument(env, cfg);
      }
      if (url.pathname.startsWith("/admin/")) {
        return await handleAdmin(request, env, cfg, url);
      }
      return json({ error: "not found" }, 404);
    } catch (err) {
      const status = err instanceof StateError ? err.status : 500;
      if (status === 500) console.error(`unhandled error on ${url.pathname}:`, err);
      else console.warn(`${url.pathname}: ${err.message}`);
      return json({ error: err.message || "internal error" }, status);
    }
  },

  /**
   * Hourly keepalive. online.py treats a signed file older than
   * FRESHNESS_SECONDS (6 h) as "the publisher pipeline is down" and fails
   * closed, so the document must be republished even when nobody buys anything.
   */
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        try {
          const result = await republish(env, repoConfig(env), "scheduled keepalive");
          console.log("scheduled publish:", JSON.stringify(result));
        } catch (err) {
          console.error("scheduled publish failed:", err.message || err);
        }
      })()
    );
  },
};

function repoConfig(env) {
  return {
    PUBLIC_REPO: env.PUBLIC_REPO || DEFAULT_PUBLIC_REPO,
    BRANCH: env.BRANCH || DEFAULT_BRANCH,
    FILE_PATH: env.FILE_PATH || DEFAULT_FILE_PATH,
    GITHUB_PAT: env.GITHUB_PAT,
  };
}

// ---------------------------------------------------------------------------

async function health(env, cfg) {
  let state = null;
  let readError = null;
  try {
    state = await readState(cfg);
  } catch (err) {
    readError = err.message;
  }

  const doc = state?.doc || emptyState();
  const generatedAt = Number(doc.generated_at || 0);
  const now = Math.floor(Date.now() / 1000);

  return {
    ok: Boolean(state) && !readError,
    service: "snappyai-entitlement-publisher",
    storage: "single JSON file in the public repo (no database)",
    state_file: `${cfg.PUBLIC_REPO}/${cfg.FILE_PATH}@${cfg.BRANCH}`,
    read_error: readError,
    rows: Object.keys(doc.rows || {}).length,
    active_rows: Object.values(doc.rows || {}).filter((r) => Number(r.until || 0) > now).length,
    seen_events: (doc.seen || []).length,
    signing_key_configured: Boolean(env.ED25519_SECRET),
    webhook_secret_configured: Boolean(env.STRIPE_WEBHOOK_SIGNING_SECRET),
    github_pat_configured: Boolean(env.GITHUB_PAT),
    ed25519_pubkey: safe(() => publicKeyB64(env.ED25519_SECRET)),
    pubkey_matches_expected: safe(() => publicKeyB64(env.ED25519_SECRET) === expectedPubkey(env)),
    generated_at: generatedAt,
    age_seconds: generatedAt ? now - generatedAt : null,
    time: now,
  };
}

function safe(fn) {
  try {
    return fn();
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Stripe
// ---------------------------------------------------------------------------
async function handleStripe(request, env, cfg) {
  const declared = request.headers.get("content-length");
  if (declared && Number(declared) > MAX_BODY_BYTES) {
    return json({ error: "payload too large" }, 413);
  }
  const raw = await request.arrayBuffer();
  if (raw.byteLength > MAX_BODY_BYTES) {
    return json({ error: "payload too large" }, 413);
  }

  try {
    await verifySignature(raw, request.headers.get("stripe-signature") || "", env.STRIPE_WEBHOOK_SIGNING_SECRET);
  } catch (err) {
    // 4xx, never 5xx: Stripe retries 5xx for three days, and a delivery with a
    // bad signature can never succeed no matter how often it is retried.
    const status = err instanceof SignatureError ? err.status : 400;
    console.warn(`stripe webhook rejected: ${err.message}`);
    return json({ error: err.message }, status);
  }

  let event;
  try {
    event = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json({ error: "body is not valid JSON" }, 400);
  }

  const eventId = String(event?.id || "");
  const eventType = String(event?.type || "");
  if (!eventId || !eventType) {
    return json({ error: "event missing id or type" }, 400);
  }
  if (!HANDLED_EVENT_TYPES.has(eventType)) {
    return json({ received: true, applied: false, reason: "event type ignored" });
  }

  // Read-modify-write with compare-and-swap retry. The whole update is one
  // transaction against one file, so two simultaneous webhooks serialise
  // instead of clobbering each other.
  for (let attempt = 1; attempt <= CAS_RETRIES; attempt++) {
    const { doc, sha } = await readState(cfg);

    if (hasSeen(doc, eventId)) {
      return json({ received: true, applied: false, reason: "already processed" });
    }

    const now = Date.now() / 1000;
    const created = Number(event.created || now);

    const email = extractEmail(event);
    const emailHash = email ? await rowKey(email) : null;
    const hash = resolveHash(doc, event, emailHash);

    if (!hash) {
      // No email and no known subscription/payment-intent mapping: we cannot
      // tell whose row this is. Acknowledge so Stripe stops retrying, but say
      // so loudly in the log -- this is a real payment we could not attribute.
      console.error(
        `event ${eventId} (${eventType}) carried no email and matched no known ` +
          `subscription id; recorded as unapplied. Check the Stripe payload.`
      );
      markSeen(doc, eventId);
      doc.unkeyed = (doc.unkeyed || []).concat([`${eventId}:${eventType}`]).slice(-200);
      await commit(env, cfg, doc, sha, `unkeyed event ${eventType}`);
      return json({ received: true, applied: false, reason: "no customer email" });
    }

    const existing = doc.rows?.[hash] || { status: "unsubscribed", plan: null, until: 0, refunded_at: 0 };
    const updated = applyEventToRow(existing, event, now, created);
    // applyEventToRow returns a freshly built row, so anything not part of the
    // state machine has to be carried across explicitly. `shared` is set by
    // presence-based sharing detection, not by Stripe events; without this it
    // would be wiped by the customer's next payment event and a flagged account
    // would silently un-flag itself.
    if (existing.shared) updated.shared = true;

    doc.rows = { ...(doc.rows || {}), [hash]: updated };
    rememberIds(doc, event, hash);
    markSeen(doc, eventId);
    doc.cursor = Math.max(Number(doc.cursor || 0), created);
    prune(doc, now, PRUNE_AFTER);

    const result = await commit(env, cfg, doc, sha, `live entitlements (${eventType})`);
    if (result.conflict) {
      console.warn(`concurrent publish, retrying (attempt ${attempt}/${CAS_RETRIES})`);
      continue;
    }

    return json({
      received: true,
      applied: true,
      event_type: eventType,
      status: updated.status,
      active: isActive(updated, now),
      until: Math.trunc(updated.until),
      published: Boolean(result.ok),
      attempt,
    });
  }

  // Exhausted retries: another writer kept winning. Return 500 so Stripe
  // retries -- this is a genuine transient failure, unlike a bad signature.
  return json({ error: "publish kept conflicting; Stripe should retry" }, 500);
}

/** Sign, self-verify, guard the row floor, and write. */
async function commit(env, cfg, doc, sha, message) {
  const now = Date.now() / 1000;
  const previousCount = Number(doc.previous_row_count || 0);
  const nextRows = toPublicRows(doc);

  const floor = checkRowFloor(previousCount, Object.keys(nextRows).length);
  if (floor) {
    // Refuse rather than publish a mass revocation. This is the guard the old
    // pipeline needed because a clobbered state.json once signed rows:{} and
    // revoked every customer at their next re-check.
    console.error(floor);
    throw new StateError(floor, 500);
  }

  doc.generated_at = Math.trunc(now);
  doc.cursor = Math.max(Number(doc.cursor || 0), Math.trunc(now));
  doc.rows_out = undefined;
  delete doc.rows_out;

  const signed = signDocument(
    {
      v: 1,
      generated_at: doc.generated_at,
      cursor: doc.cursor,
      rows: nextRows,
      seen: doc.seen || [],
      subs: doc.subs || {},
      pis: doc.pis || {},
      ...(doc.unkeyed ? { unkeyed: doc.unkeyed } : {}),
    },
    env.ED25519_SECRET,
    expectedPubkey(env)
  );

  // Verify our own output before it goes anywhere near a client. Turns "someone
  // rotated the secret and forgot the clients" from a silent total lockout into
  // a loud failure.
  const pubkey = expectedPubkey(env);
  if (!verifyDocument(signed, pubkey)) {
    throw new StateError("refusing to publish: the document we just signed does not verify", 500);
  }
  if (signed.rows && Object.keys(signed.rows).length !== Object.keys(nextRows).length) {
    throw new StateError("row count changed during signing; refusing to publish", 500);
  }

  // Remember the count we just published, so the next run can compare against it.
  doc.previous_row_count = Object.keys(nextRows).length;

  return writeState(cfg, canonicalJson(signed), sha, message);
}

async function republish(env, cfg, message) {
  for (let attempt = 1; attempt <= CAS_RETRIES; attempt++) {
    const { doc, sha } = await readState(cfg);
    const result = await commit(env, cfg, doc, sha, message);
    if (!result.conflict) {
      return { ok: Boolean(result.ok), rows: Object.keys(toPublicRows(doc)).length, attempt };
    }
  }
  throw new StateError(`republish kept conflicting after ${CAS_RETRIES} attempts`);
}

function canonicalJson(doc) {
  return JSON.stringify(sortDeep(doc));
}

function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((k) => [k, sortDeep(value[k])])
    );
  }
  return value;
}

async function serveDocument(env, cfg) {
  const { doc } = await readState(cfg);
  const now = Date.now() / 1000;
  const signed = signDocument(
    {
      v: 1,
      generated_at: Math.trunc(now),
      cursor: Number(doc.cursor || 0),
      rows: toPublicRows(doc),
    },
    env.ED25519_SECRET,
    expectedPubkey(env)
  );
  return new Response(canonicalJson(signed), {
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "public, max-age=30",
      "x-content-type-options": "nosniff",
    },
  });
}

// ---------------------------------------------------------------------------
// Admin
// ---------------------------------------------------------------------------
async function handleAdmin(request, env, cfg, url) {
  if (!env.ADMIN_TOKEN) return json({ error: "admin surface disabled" }, 503);
  if (!fixedTimeEqual(request.headers.get("x-admin-token") || "", env.ADMIN_TOKEN)) {
    return json({ error: "invalid admin token" }, 401);
  }

  const path = url.pathname;
  const now = Date.now() / 1000;

  if (request.method === "GET" && path === "/admin/lookup") {
    const email = url.searchParams.get("email");
    const hash = url.searchParams.get("hash") || (email ? await rowKey(email) : null);
    if (!hash) return json({ error: "provide ?email= or ?hash=" }, 400);

    const { doc } = await readState(cfg);
    const row = (doc.rows || {})[hash] || null;
    return json({
      found: Boolean(row),
      email_hash: hash,
      ...(row
        ? {
            status: row.status,
            plan: row.plan ?? null,
            until: Math.trunc(Number(row.until || 0)),
            active: isActive(row, now),
          }
        : {}),
      known_subscriptions: Object.entries(doc.subs || {})
        .filter(([, h]) => h === hash)
        .map(([subId]) => subId),
    });
  }

  if (request.method === "POST" && path === "/admin/grant") {
    const body = await request.json().catch(() => null);
    const email = String(body?.email || "").trim();
    const days = Number(body?.days || 0);
    const reason = String(body?.reason || "").trim();
    if (!email || !Number.isFinite(days) || days <= 0 || days > 3650 || reason.length < 3) {
      return json({ error: "need email, days (1-3650) and a reason of 3+ chars" }, 400);
    }

    for (let attempt = 1; attempt <= CAS_RETRIES; attempt++) {
      const { doc, sha } = await readState(cfg);
      const hash = await rowKey(email);
      const existing = (doc.rows || {})[hash] || { status: "unsubscribed", plan: null, until: 0, refunded_at: 0 };
      const updated = supportGrant(existing, now, days, reason);
      doc.rows = { ...(doc.rows || {}), [hash]: updated };
      doc.grants = { ...(doc.grants || {}), [hash]: `${new Date(now * 1000).toISOString()} +${days}d ${reason}` };

      const result = await commit(env, cfg, doc, sha, `support grant (${days}d)`);
      if (result.conflict) continue;
      return json({
        email_hash: hash,
        status: updated.status,
        until: Math.trunc(updated.until),
        active: isActive(updated, now),
        published: Boolean(result.ok),
      });
    }
    return json({ error: "kept conflicting; try again" }, 500);
  }

  if (request.method === "POST" && path === "/admin/publish") {
    return json(await republish(env, cfg, "manual publish"));
  }

  if (request.method === "GET" && path === "/admin/state") {
    const { doc, sha } = await readState(cfg);
    return json({
      sha,
      generated_at: doc.generated_at,
      cursor: doc.cursor,
      rows: Object.keys(doc.rows || {}).length,
      seen: (doc.seen || []).length,
      subs: Object.keys(doc.subs || {}).length,
      pis: Object.keys(doc.pis || {}).length,
    });
  }

  return json({ error: "not found" }, 404);
}

function fixedTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
