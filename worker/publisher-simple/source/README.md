# snappyai-publisher — no-database edition

Stripe webhook in, signed `entitlements.json` out, written to your public repo.
**Storage is one JSON file. There is no database, no KV namespace, no schema and
no migration.**

Start at **[SETUP.md](SETUP.md)** — it's copy-paste all the way through.

## Two deployment shapes, one codebase

| | who it's for | what runs | config |
|---|---|---|---|
| **Dashboard upload** (recommended) | no terminal at all | `dashboard/worker.js` — everything bundled into ONE file | 4 secrets in the dashboard UI; zero vars (defaults baked in) |
| **wrangler CLI** | if you ever want it | `src/worker.js` + `wrangler.toml` | same 4 secrets; vars already filled in |

The Cloudflare dashboard's upload flow **ignores `wrangler.toml`** and only
runs a root `worker.js` it can't resolve imports from — that's why
`dashboard/worker.js` exists (built by `build-bundle.mjs`, covered by
`test/bundle.test.js`). Whichever route deploys last wins; don't mix them.

---

## Why there's no database

The previous edition used Cloudflare D1, which meant running `wrangler d1
create`, finding a `database_id`, pasting it into `wrangler.toml`, applying a
`schema.sql`, and keeping the database and the published file in step. That's
real complexity for a system whose entire state is a few hundred small rows.

Your old design had the right instinct: the state lives in a JSON file. The only
thing that made it need a *second* file (`state.json` in the private repo) was
that `builder.py` published a document containing only
`{v, generated_at, cursor, rows, sig}` — so the bookkeeping had nowhere else to
go.

This edition puts the bookkeeping **in the same signed file**:

| key | what it's for | public? |
|---|---|---|
| `v`, `generated_at`, `cursor`, `rows`, `sig` | exactly as before | yes, as before |
| `seen` | last 500 event ids, so a replay can't be applied twice | newly public |
| `subs` | `sub_... → row hash`, so a cancellation with no email finds its row | newly public |
| `pis` | `pi_... → row hash`, same for refunds | newly public |

Stripe ids aren't credentials — they're useless without your secret key — so
this costs you some metadata you'd rather not publish, and buys you zero
infrastructure. `rows` is unchanged in kind: still `sha256("email:"+address)`
keys in a public file, which is the exposure you already accepted.

One file is also *atomically consistent by construction*. Two files need a
transaction; one file either writes or doesn't.

---

## Layout

```
publisher-simple/
├── SETUP.md                 start here
├── wrangler.toml            ONE placeholder to fill: your public key
├── check-deploy.sh          pre-flight diagnostic; run it before deploying
├── edsig.py                 your client's module, unmodified, for the interop test
├── src/
│   ├── worker.js            routes, Stripe handling, admin
│   ├── state-file.js        the JSON file: read, CAS write, transitions
│   ├── entitlements.js      state machine, ported from builder.py
│   ├── signing.js           canonicalisation + Ed25519
│   ├── stripe.js            webhook signature verification
│   └── vendor/tweetnacl.js  vendored ESM (Unlicense) — so no npm install
└── test/
    ├── publisher.test.js    42 tests
    ├── github-stub.js       faithful Contents API stub, including 409 on stale sha
    └── verify-with-edsig.py verifies Worker output with YOUR edsig.py
```

`src/entitlements.js`, `signing.js`, `stripe.js` and the vendored tweetnacl are
shared with `publisher-worker/` (the D1 edition). The Worker and the storage
layer are new.

---

## Running it

```bash
bash check-deploy.sh   # tells you what's wrong before Cloudflare does
npm test               # 42 tests, includes cross-language edsig.py verification
wrangler deploy        # from INSIDE this directory
wrangler tail          # watch events land
```

No `npm install`. tweetnacl is vendored as ESM because it ships CommonJS only,
and an ES-module Worker can't resolve a bare `import nacl from "tweetnacl"`.

---

## Correctness notes worth knowing

**Stripe Basil.** `current_period_end` moved off the Subscription object onto
`items.data[n]`. Reading only the top level returns 0, so the first checkout
binds and every renewal silently fails to extend. Both shapes are handled and
tested.

**`customer.subscription.*` carries no email.** The object *is* the
subscription, so its id is `data.object.id`, not `data.object.subscription.id`.
Missing that shape means cancellations land in `unkeyed` and the customer keeps
access they stopped paying for. All three id shapes are tested.

**Refund resurrection.** `refunded_at` must survive a publish/read cycle, or
`bindSubscription`'s `status == "refunded" && now <= refunded_at` guard is dead
and a replayed older checkout un-refunds a subscription. Tested.

**The `shared` flag.** Set by presence-based sharing detection, not by Stripe
events. Rebuilding a row from an event wiped it — and `online.py` line 1336
reads `shared`. Carried across explicitly. Tested.

**Row floor.** Refuses to publish when rows collapse to zero or below 40% of the
previous count. The old pipeline needed this because a clobbered `state.json`
once signed `rows: {}` and revoked every customer at their next re-check.

**Compare-and-swap.** Writes carry the blob sha, so concurrent webhooks
serialise instead of clobbering. The old `git push --force` needed a
`concurrency` group to approximate this.

**Deterministic Ed25519.** RFC 8032, so tweetnacl and `edsig.py` produce
identical signatures. Proven by `test/verify-with-edsig.py`, which imports your
unmodified `edsig.py` and verifies real Worker output.

---

## What this is not

Not the one-machine-at-a-time rule. That's still detection via the client's
presence ping, not prevention. Prevention needs real-time state somewhere — a
Cloudflare Durable Object on the same free account would do it. Separate work.

Not the licence-server design either. `merged/snappyai-backend/` holds a Path B
server that isn't running anywhere because you have no host. This is the design
that matches your actual constraints.

`publisher-worker/` (the D1 edition) is superseded. Kept for its tests and
notes; **deploy this directory instead.**
