/**
 * Builds the SINGLE-FILE worker that the Cloudflare dashboard understands.
 *
 *   node build-bundle.mjs        (requires `npm install` once, for esbuild)
 *
 * Why this exists: the dashboard's "Upload files" flow IGNORES wrangler.toml
 * and only runs a root-level `worker.js`. It cannot resolve `import ... from
 * "./src/..."` either. So we bundle src/worker.js and every module it imports
 * (including the vendored tweetnacl) into one self-contained ES module:
 *
 *   ../worker.js   (archive layout: worker/publisher-simple/worker.js)
 *
 * The pre-built file is committed. You only need to run this script if you
 * edit something under src/.
 */
import { build } from "esbuild";

/**
 * SAFETY NET: upstream tweetnacl's environment probe had a Node.js-only
 * `require('crypto')` branch. Our vendored copy has it REMOVED (Workers always
 * have crypto.getRandomValues — see the PATCHED note in src/vendor/tweetnacl.js),
 * so this plugin never fires in a normal build. It stays so that if tweetnacl
 * is ever re-vendored from upstream, esbuild still resolves instead of dying,
 * and the editor never sees a `__require` shim.
 */
const stubNodeCrypto = {
  name: "stub-node-crypto",
  setup(b) {
    b.onResolve({ filter: /^crypto$/ }, () => ({ path: "crypto", namespace: "stub" }));
    b.onLoad({ filter: /.*/, namespace: "stub" }, () => ({ contents: "export default {}", loader: "js" }));
  },
};

await build({
  plugins: [stubNodeCrypto],
  entryPoints: ["src/worker.js"],
  outfile: "../worker.js",
  bundle: true,
  format: "esm",
  target: "es2022",
  platform: "neutral",
  mainFields: ["module", "main"],
  banner: {
    js: [
      "// @ts-nocheck",
      "//   ^ Silences the Cloudflare dashboard editor's TypeScript checker on",
      "//     this GENERATED file. The real source in ../src/ is type-checked",
      "//     (tsconfig.editorcheck.json); re-checking machine-generated code",
      "//     adds no safety and only produces red squiggles in the editor.",
      "/* =====================================================================",
      " * SnappyAI entitlement publisher — SINGLE-FILE dashboard build.",
      " * GENERATED FILE — do not edit by hand. Source lives in source/src/",
      " * (archive layout: worker/publisher-simple/source/).",
      " * Rebuild with: npm install && node build-bundle.mjs",
      " *",
      " * Deploy: Cloudflare dashboard → your Worker → Upload this one file.",
      " * wrangler.toml is NOT needed for this build. Defaults are baked in:",
      " *   repo  gerykurucz/snappyai-entitlements",
      " *   branch live, file entitlements.json",
      " *   expected pubkey J3Ulxeu/JPzIlGypJ28+h45rlw3aW6bJFfDu3luRGOE=",
      " * You must still set 4 secrets in the dashboard (see docs/WORKER-SETUP.md).",
      " * ===================================================================== */",
    ].join("\n"),
  },
});

console.log("wrote ../worker.js  (worker/publisher-simple/worker.js)");
