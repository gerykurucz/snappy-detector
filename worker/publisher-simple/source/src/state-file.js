/**
 * The state file: one JSON document in the public repo, and nothing else.
 *
 * This is the whole storage layer. There is no database, no KV namespace, no
 * migration, and nothing to create in the Cloudflare dashboard beyond the Worker
 * itself. The file holds both the published entitlements AND the bookkeeping
 * needed to maintain them, which is what removes the need for anywhere else to
 * put state:
 *
 *   {
 *     "v": 1,
 *     "generated_at": 1790980695,     // freshness clock; clients fail closed at 6 h
 *     "cursor":       1790980695,     // Stripe event cursor
 *     "rows":  { "<sha256(email:addr)>": {until, status, plan}, ... },
 *     "seen":  [ "evt_...", ... ],    // idempotency; capped at SEEN_LIMIT
 *     "subs":  { "sub_...": "<row hash>" },   // so a cancellation can find its row
 *     "pis":   { "pi_...":  "<row hash>" },
 *     "sig":   "..."                  // Ed25519 over everything above
 *   }
 *
 * WHY THE EXTRA KEYS DO NOT BREAK SHIPPED CLIENTS
 * online._canonical_bytes() canonicalises over EVERY key except `sig` -- its own
 * comment says it stopped enumerating (v, generated_at, cursor, rows) precisely
 * so that "the first time anyone added a top-level field to the signed document
 * (a note, a schema, a min_version)" clients would not all start rejecting the
 * file. So `seen`/`subs`/`pis` are covered by the signature and ignored by the
 * client, which reads only `rows` and `generated_at`.
 *
 * WHY ONE FILE AND NOT TWO
 * Two files (a private state plus a public document) is what the old design did,
 * and it needs somewhere private to live plus a way to keep the two in step.
 * One file is atomically consistent by construction: the write either happens or
 * it does not.
 *
 * WHAT THIS EXPOSES, HONESTLY
 * The repo is public, so `subs` (Stripe subscription ids) and `seen` (event ids)
 * become public too. Neither is a credential -- a Stripe id is useless without
 * your secret key -- but they are metadata you would not otherwise publish. Row
 * keys remain sha256("email:"+address), which is the exposure you already
 * accepted: crackable against an email wordlist, so your subscriber count is
 * effectively public. See SETUP.md for how to remove that later.
 *
 * CONCURRENCY
 * Writes go through the GitHub Contents API with the file's blob SHA, which makes
 * them a compare-and-swap. Two webhooks arriving at once cannot clobber each
 * other: the loser gets a 409, re-reads, re-applies and retries. That is strictly
 * better than the old `git push --force`, which needed a `concurrency` group and
 * a ROW_FLOOR_KEEP guard because a lost update could publish `rows: {}` and
 * revoke every customer at once.
 */

const SEEN_LIMIT = 500;
const API = "https://api.github.com";

export class StateError extends Error {
  constructor(message, status = 502) {
    super(message);
    this.name = "StateError";
    this.status = status;
  }
}

export function emptyState(now = 0) {
  return {
    v: 1,
    generated_at: Math.trunc(now),
    cursor: 0,
    rows: {},
    seen: [],
    subs: {},
    pis: {},
  };
}

/** True when a freshly created repo holds the placeholder we ship. */
export function isPlaceholder(doc) {
  return (
    !doc ||
    typeof doc !== "object" ||
    doc.generated_at === 0 ||
    (!doc.rows && !doc.seen)
  );
}

async function gh(path, cfg, init = {}) {
  if (!cfg.GITHUB_PAT) {
    throw new StateError("GITHUB_PAT is not configured", 503);
  }
  return fetch(`${API}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${cfg.GITHUB_PAT}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "snappyai-entitlement-publisher",
      ...(init.headers || {}),
    },
  });
}

function contentPath(cfg) {
  return `/repos/${cfg.PUBLIC_REPO}/contents/${cfg.FILE_PATH || "entitlements.json"}?ref=${
    cfg.BRANCH || "live"
  }`;
}

/**
 * Read the state file. Returns { doc, sha, missing }.
 *
 * `sha` is the blob SHA needed for a compare-and-swap write. `missing` is true
 * when the file does not exist yet, in which case a fresh state is returned and
 * the first write will CREATE it.
 */
export async function readState(cfg) {
  const resp = await gh(contentPath(cfg), cfg);

  if (resp.status === 404) {
    return { doc: emptyState(Date.now() / 1000), sha: null, missing: true };
  }
  if (!resp.ok) {
    const text = (await resp.text()).slice(0, 300);
    if (resp.status === 403) {
      throw new StateError(
        `GitHub refused to read ${cfg.PUBLIC_REPO}/${cfg.FILE_PATH || "entitlements.json"} ` +
          `(403). The PAT needs Contents: read and write on that repo. ${text}`
      );
    }
    throw new StateError(`could not read the state file (HTTP ${resp.status}): ${text}`, resp.status);
  }

  const body = await resp.json();
  const raw = atob(body.content || "");
  let doc;
  try {
    doc = JSON.parse(raw);
  } catch {
    throw new StateError("the state file is not valid JSON; refusing to overwrite it");
  }
  if (isPlaceholder(doc)) {
    // The placeholder we ask you to commit in SETUP.md. Replace it in memory so
    // the first publish produces a real document, but keep the sha so the write
    // is still a compare-and-swap.
    doc = { ...emptyState(Date.now() / 1000), ...{ rows: {} } };
  }
  return { doc, sha: body.sha || null, missing: false };
}

/**
 * Write the file. `expectedSha` makes it a compare-and-swap: if the file changed
 * since we read it, GitHub answers 409 and the caller must re-read and retry.
 */
export async function writeState(cfg, contentText, expectedSha, message) {
  const payload = {
    message,
    content: btoa(unescape(encodeURIComponent(contentText))),
    branch: cfg.BRANCH || "live",
  };
  if (expectedSha) payload.sha = expectedSha;

  const resp = await gh(`/repos/${cfg.PUBLIC_REPO}/contents/${cfg.FILE_PATH || "entitlements.json"}`, cfg, {
    method: "PUT",
    body: JSON.stringify(payload),
  });

  if (resp.ok) {
    const body = await resp.json();
    return { ok: true, sha: body?.content?.sha || null, status: resp.status };
  }

  const text = (await resp.text()).slice(0, 300);
  if (resp.status === 409 || /does not match|conflict/i.test(text)) {
    return { ok: false, conflict: true };
  }
  if (resp.status === 404) {
    throw new StateError(
      `GitHub returned 404 for ${cfg.PUBLIC_REPO}/${cfg.FILE_PATH || "entitlements.json"} on ` +
        `branch ${cfg.BRANCH || "live"}. The repo must exist, must be PUBLIC, and the ` +
        `branch must exist. ${text}`
    );
  }
  if (resp.status === 403 || resp.status === 429) {
    throw new StateError(
      `GitHub refused the write (HTTP ${resp.status}). Check that GITHUB_PAT is a ` +
        `fine-grained token with Contents: read and write on ${cfg.PUBLIC_REPO} only. ${text}`
    );
  }
  throw new StateError(`write failed (HTTP ${resp.status}): ${text}`);
}

// ---------------------------------------------------------------------------
// state transitions
// ---------------------------------------------------------------------------

/** Bound the idempotency list, exactly as builder.prune_state did. */
export function trimSeen(state, limit = SEEN_LIMIT) {
  if (Array.isArray(state.seen) && state.seen.length > limit) {
    state.seen = state.seen.slice(-limit);
  }
  return state;
}

export function hasSeen(state, eventId) {
  return Array.isArray(state.seen) && state.seen.includes(eventId);
}

export function markSeen(state, eventId) {
  if (!Array.isArray(state.seen)) state.seen = [];
  if (!state.seen.includes(eventId)) state.seen.push(eventId);
  return trimSeen(state);
}

/** Drop rows dead for PRUNE_AFTER and the subs/pis entries pointing at them. */
export function prune(state, now, pruneAfter) {
  const rows = state.rows || {};
  for (const key of Object.keys(rows)) {
    const ent = rows[key];
    const until = Number((ent && ent.until) || 0);
    if (!ent || typeof ent !== "object" || (until && until < now - pruneAfter)) {
      delete rows[key];
    }
  }
  const live = new Set(Object.keys(rows));
  for (const map of [state.subs, state.pis]) {
    if (!map || typeof map !== "object") continue;
    for (const key of Object.keys(map)) {
      if (!live.has(map[key])) delete map[key];
    }
  }
  return trimSeen(state);
}

/**
 * The rows as published.
 *
 * This file is BOTH the published document and the state, so the row shape has
 * to carry the fields the state machine needs on the next read -- not just the
 * three builder.build_document emitted. Dropping `refunded_at` here silently
 * disabled the refund-resurrection guard: bindSubscription() and
 * markRefunded() both test `status === "refunded" && now <= refunded_at`, so
 * without it a replayed older checkout could un-refund a subscription after any
 * restart.
 *
 * `sub_id` is still stripped: the sub -> row mapping lives in `subs`, and a
 * Stripe subscription id is metadata with no business being world-readable.
 */
export function toPublicRows(state) {
  const out = {};
  for (const [hash, ent] of Object.entries(state.rows || {})) {
    if (!ent || typeof ent !== "object") continue;
    const row = {
      until: Math.trunc(Number(ent.until || 0)),
      status: ent.status || "unsubscribed",
      plan: ent.plan ?? null,
      refunded_at: Math.trunc(Number(ent.refunded_at || 0)),
    };
    if (ent.shared) row.shared = true;
    if (ent.note) row.note = String(ent.note).slice(0, 200);
    out[hash] = row;
  }
  return out;
}

/**
 * Which subscription id does this event refer to?
 *
 * Three shapes, and getting only the first one is a live bug:
 *
 *   invoice.paid / invoice.payment_failed   object.subscription is an expanded
 *                                           Subscription object
 *   checkout.session.completed              object.subscription is a bare id string
 *   customer.subscription.*                 the object IS the Subscription, so the
 *                                           id is object.id -- there is no
 *                                           .subscription key at all
 *
 * That last shape is the one that matters most: customer.subscription.deleted and
 * .updated are how a cancellation or downgrade reaches us, and Stripe sends them
 * WITHOUT customer_email. If they are not resolved, the event lands in `unkeyed`
 * and the customer keeps paying access they no longer paid for.
 */
export function subscriptionIdOf(event) {
  const obj = (event && event.data && event.data.object) || {};

  const sub = obj.subscription;
  if (typeof sub === "string" && sub.startsWith("sub_")) return sub;
  if (sub && typeof sub === "object" && typeof sub.id === "string") return sub.id;

  if (typeof obj.id === "string" && obj.id.startsWith("sub_")) return obj.id;

  return null;
}

/** Resolve the row hash for an event, using subs/pis when the email is absent. */
export function resolveHash(state, event, emailHash) {
  const obj = (event && event.data && event.data.object) || {};

  const subId = subscriptionIdOf(event);
  if (subId && state.subs && state.subs[subId]) return state.subs[subId];
  if (obj.payment_intent && state.pis && state.pis[obj.payment_intent]) {
    return state.pis[obj.payment_intent];
  }
  return emailHash || null;
}

/** Remember the subscription/payment-intent -> row mapping for later events. */
export function rememberIds(state, event, hash) {
  const obj = (event && event.data && event.data.object) || {};
  const subId = subscriptionIdOf(event);
  if (subId) {
    state.subs = state.subs || {};
    state.subs[subId] = hash;
  }
  if (obj.payment_intent) {
    state.pis = state.pis || {};
    state.pis[obj.payment_intent] = hash;
  }
  return state;
}
