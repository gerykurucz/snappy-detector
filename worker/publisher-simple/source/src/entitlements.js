/**
 * Entitlement state machine — a direct port of builder.py.
 *
 * Pure functions over plain objects: no D1, no fetch, no crypto. That is
 * deliberate, because these rules are the ones that decide whether a paying
 * customer has access, and they must be testable without a Cloudflare account.
 *
 * Every precedence rule from builder.py is preserved, including the ones that
 * only exist because of a past incident:
 *
 *   - a refund is un-revoked ONLY by a checkout newer than the refund;
 *   - a live paid month, a paid-but-cancelling month, or a support grant all
 *     outrank a cheap trial (the old rule only protected "active", so a trial
 *     purchase relabelled a paying canceller as "trial");
 *   - `canceling` keeps access until the paid period end;
 *   - events apply with their OWN timestamps, so replay and out-of-order
 *     delivery are harmless.
 */

export const DAY = 86400;
export const TRIAL_SECONDS = 2 * DAY;
export const ACTIVE_STATUSES = ["trial", "active", "grant", "canceling"];
export const PRUNE_AFTER = 90 * DAY;

/** Refuse to publish a file that lost this much of its paid base in one run.
 * A clobbered state once signed and published `rows: {}`, which revoked every
 * customer at their next re-check. */
export const ROW_FLOOR_KEEP = 0.5;

export function recompute(ent, now) {
  const until = Number(ent.until || 0);
  const status = ent.status || "unsubscribed";
  return {
    ...ent,
    until,
    status,
    active: until > now && ACTIVE_STATUSES.includes(status),
  };
}

export function isActive(ent, now) {
  if (!ent) return false;
  return Number(ent.until || 0) > now && ACTIVE_STATUSES.includes(ent.status);
}

export function grantTrial(ent, now, trialSeconds = TRIAL_SECONDS) {
  if (ent.status === "refunded" && now <= Number(ent.refunded_at || 0)) {
    return ent; // a replayed trial cannot un-refund
  }
  if (ent.until > now && ["active", "canceling", "grant"].includes(ent.status)) {
    return ent; // a paid month or a support grant outranks a trial
  }
  const base = Number(ent.until || 0);
  return recompute(
    {
      ...ent,
      status: "trial",
      plan: "trial",
      until: Math.max(base > now ? base : 0, now + trialSeconds),
      refunded_at: 0,
      updated: now,
    },
    now
  );
}

export function bindSubscription(ent, now, periodEnd, subStatus, subId) {
  if (ent.status === "refunded" && now <= Number(ent.refunded_at || 0)) {
    // Only a checkout NEWER than the refund may resurrect a revoked row.
    return ent;
  }
  const status = { trialing: "trial", active: "active" }[subStatus] || "past_due";
  return recompute(
    {
      ...ent,
      status,
      plan: status === "trial" ? "trial" : "monthly",
      until: Math.max(Number(ent.until || 0), Number(periodEnd || 0)),
      sub_id: subId,
      refunded_at: 0,
      updated: now,
    },
    now
  );
}

export function extendPeriod(ent, now, periodEnd) {
  if (!periodEnd) return ent;
  if (ent.status === "refunded") return ent; // refunded stays revoked
  const status = ent.status === "canceled" ? "active" : ent.status || "active";
  return recompute(
    {
      ...ent,
      status,
      plan: ent.plan || "monthly",
      until: Math.max(Number(ent.until || 0), Number(periodEnd)),
      updated: now,
    },
    now
  );
}

export function syncSubscription(ent, now, periodEnd, subStatus, cancelAtPeriodEnd = false) {
  if (ent.status === "refunded") return ent;

  if (subStatus === "active" || subStatus === "trialing") {
    let next = bindSubscription(ent, now, periodEnd, subStatus, ent.sub_id);
    if (cancelAtPeriodEnd) {
      // Cancelled: access runs to the paid period end, but the publisher and
      // the gate now know it will not renew.
      return recompute({ ...next, status: "canceling", updated: now }, now);
    }
    return next;
  }
  if (subStatus === "past_due") {
    return recompute({ ...ent, status: "past_due", updated: now }, now);
  }
  if (["canceled", "unpaid", "paused", "incomplete_expired"].includes(subStatus)) {
    return recompute(
      {
        ...ent,
        status: "canceled",
        sub_id: null,
        until: Math.min(Number(ent.until || 0), now),
        updated: now,
      },
      now
    );
  }
  return ent;
}

export function cancel(ent, now) {
  return recompute(
    {
      ...ent,
      status: "canceled",
      sub_id: null,
      until: Math.min(Number(ent.until || 0), now),
      updated: now,
    },
    now
  );
}

export function pastDue(ent, now) {
  return recompute({ ...ent, status: "past_due", updated: now }, now);
}

export function markRefunded(ent, now) {
  return recompute(
    {
      ...ent,
      status: "refunded",
      refunded_at: now,
      until: Math.min(Number(ent.until || 0), now),
      sub_id: null,
      updated: now,
    },
    now
  );
}

export function supportGrant(ent, now, days, reason) {
  return recompute(
    {
      ...ent,
      until: Math.max(Number(ent.until || 0), now) + days * DAY,
      status: ["active", "canceling", "grant"].includes(ent.status) ? ent.status : "grant",
      plan: ent.plan || "grant",
      note: reason,
      refunded_at: 0,
      updated: now,
    },
    now
  );
}

/**
 * The billing period end, across Stripe API versions.
 *
 * Stripe's Basil release (2025-03-31) removed `current_period_end` from the
 * Subscription object; it now lives on `subscription.items.data[n]`. Reading
 * only the top-level field returns 0 on a current API version, and then every
 * renewal silently fails to extend -- the customer loses access a month after
 * deploy despite having paid, which presents as their fault rather than ours.
 */
export function extractPeriodEnd(event, sub) {
  const candidates = [];
  const collect = (value) => {
    if (typeof value === "number" && value > 0) candidates.push(value);
  };

  const items = sub && sub.items && Array.isArray(sub.items.data) ? sub.items.data : [];
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    collect(item.current_period_end);
    if (item.period && typeof item.period === "object") collect(item.period.end);
  }

  // Accounts pinned to a pre-Basil API version still send it at the top level.
  collect(sub && sub.current_period_end);

  const obj = (event && event.data && event.data.object) || {};
  const lines = obj.lines && Array.isArray(obj.lines.data) ? obj.lines.data : [];
  for (const line of lines) {
    if (line && line.period && typeof line.period === "object") collect(line.period.end);
  }

  return candidates.length ? Math.max(...candidates) : 0;
}

/** The subscription object, however this event shape carries it. */
export function extractSubscription(event) {
  const obj = (event && event.data && event.data.object) || {};
  const sub = obj.subscription;
  if (sub && typeof sub === "object") return sub;
  if (typeof sub === "string" && sub) return { id: sub };
  return obj;
}

/** Best-effort subscriber email from any of the handled event shapes. */
export function extractEmail(event) {
  const obj = (event && event.data && event.data.object) || {};
  const customer = obj.customer;
  const candidates = [
    obj.customer_email,
    obj.customer_details && obj.customer_details.email,
    obj.receipt_email,
    customer && typeof customer === "object" ? customer.email : null,
    obj.billing_email,
  ];
  for (const value of candidates) {
    if (typeof value === "string" && value.trim()) return value.trim().toLowerCase();
  }
  return null;
}

/**
 * sha256("email:" + address), hex — the row key.
 *
 * MUST stay byte-identical to builder.row_key() and online.row_hash_legacy(),
 * or every published row becomes unreachable by every client. Note there is no
 * pepper: builder.py never applied one, and the client falls back to the
 * unpeppered form, so adding one here would silently orphan every customer.
 */
export async function rowKey(email, subtle = crypto.subtle) {
  const normalised = "email:" + String(email).trim().toLowerCase();
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(normalised));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Apply one Stripe event to one row. Pure: returns the new row.
 *
 * `created` is the event's own timestamp, not wall-clock now, so a replayed or
 * backfilled event cannot shift the horizon relative to what Stripe recorded.
 */
export function applyEventToRow(row, event, now, created) {
  const sub = extractSubscription(event);
  const periodEnd = extractPeriodEnd(event, sub);
  const subStatus = String(sub.status || "");
  const cancelAtPeriodEnd = Boolean(sub.cancel_at_period_end);

  switch (event.type) {
    case "checkout.session.completed":
      return sub.id
        ? bindSubscription(row, created, periodEnd, subStatus || "active", sub.id)
        : grantTrial(row, created);

    case "invoice.paid": {
      let next = periodEnd
        ? extendPeriod(row, created, periodEnd)
        : grantTrial(row, created);
      if (next.status === "past_due") {
        // A successful payment clears a past_due flag.
        next = recompute({ ...next, status: "active" }, created);
      }
      return next;
    }

    case "invoice.payment_failed":
      return pastDue(row, created);

    case "customer.subscription.updated":
      return syncSubscription(row, created, periodEnd, subStatus, cancelAtPeriodEnd);

    case "customer.subscription.deleted":
      return cancel(row, created);

    case "charge.refunded":
    case "charge.dispute.created":
      return markRefunded(row, created);

    default:
      return row;
  }
}

/** True when this event type can change a row. Anything else is noise. */
export const HANDLED_EVENT_TYPES = new Set([
  "checkout.session.completed",
  "invoice.paid",
  "invoice.payment_failed",
  "customer.subscription.updated",
  "customer.subscription.deleted",
  "charge.refunded",
  "charge.dispute.created",
]);

/** The public document, in exactly the shape builder.build_document emitted. */
export function buildDocument(rows, now, sharedHashes = new Set()) {
  const out = {};
  for (const [hash, ent] of Object.entries(rows)) {
    const until = Number(ent.until || 0);
    if (until && until < now - PRUNE_AFTER) continue; // long-dead: prune
    const row = {
      until: Math.trunc(until),
      status: ent.status || "unsubscribed",
      plan: ent.plan ?? null,
    };
    if (sharedHashes.has(hash)) row.shared = true;
    out[hash] = row;
  }
  return { v: 1, generated_at: Math.trunc(now), cursor: Math.trunc(now), rows: out };
}

/**
 * Guard against publishing a file that just revoked most of the customer base.
 * Returns null when the shrink is too large to be believable.
 */
export function checkRowFloor(previousCount, nextCount) {
  if (previousCount <= 0) return null;
  if (nextCount >= previousCount * ROW_FLOOR_KEEP) return null;
  return (
    `refusing to publish: rows fell from ${previousCount} to ${nextCount} ` +
    `(< ${ROW_FLOOR_KEEP * 100}% kept). This is what a clobbered state looks ` +
    `like, and publishing it would revoke every customer at their next re-check.`
  );
}
