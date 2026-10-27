
import argparse
import ast
import hashlib
import os
import sys

BLOB_MODULE = "general_utils.py"
MANIFEST_NAME = "SHA256SUMS.txt"

_RUNTIME_FILES = frozenset((
    "config.json", "config.ini", "macros.json", "pad_macros.json",
    "reticle_presets.json",
))
_RUNTIME_DIRS = frozenset(("logs",))
_RUNTIME_SUFFIXES = (".log", ".tmp", ".snapshot.json")


def _sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def read_secret_blob(project_dir):
    path = os.path.join(project_dir, BLOB_MODULE)
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    key = None
    blob = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "_KEY":
            key = ast.literal_eval(node.value)
        elif target.id == "_BLOB":
            blob = ast.literal_eval(node.value)
    if key is None:
        raise SystemExit("could not find _KEY in %s" % path)
    return key, blob


def decode_blob(key, blob):
    import base64
    ks = hashlib.sha256(key).digest()
    out = {}
    for name, raw in sorted(blob.items()):
        if not raw:
            continue
        try:
            data = base64.b64decode(raw, validate=True)
            out[name] = bytes(c ^ ks[i % len(ks)]
                              for i, c in enumerate(data)).decode("utf-8")
        except Exception as exc:
            out[name] = "<undecodable: %r>" % (exc,)
    return out


def read_env_map(project_dir):
    path = os.path.join(project_dir, BLOB_MODULE)
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "_ENV":
            return ast.literal_eval(node.value)
    raise SystemExit("could not find _ENV in %s" % path)


def encode_value(key, value):
    import base64
    ks = hashlib.sha256(key).digest()
    data = value.encode("utf-8")
    return base64.b64encode(
        bytes(c ^ ks[i % len(ks)] for i, c in enumerate(data))
    ).decode("ascii")


def _blocker_count(values):
    n = len(_blockers(values))
    if not values.get("ROW_PEPPER"):
        n += 1
    return n


def encode_blob(project_dir, clear=(), write=False):
    path = os.path.join(project_dir, BLOB_MODULE)
    key, blob = read_secret_blob(project_dir)
    envmap = read_env_map(project_dir)
    unknown = sorted(set(clear) - set(blob))
    if unknown:
        raise SystemExit("--clear: unknown field(s): %s (known: %s)"
                         % (", ".join(unknown), ", ".join(sorted(blob))))
    current = decode_blob(key, blob)
    intended = {}
    changes = []
    for name in blob:
        value = current.get(name, "")
        if name in clear:
            if value:
                changes.append((name, "cleared", value, ""))
            value = ""
        else:
            env = envmap.get(name, "")
            new = os.environ.get(env, "").strip() if env else ""
            if new and new != value:
                changes.append((name, "env %s" % env, value, new))
                value = new
        intended[name] = value

    if not changes:
        print("Nothing to do: no SNAPPYAI_* override differs from the blob "
              "and no --clear field is populated.")
        return 0

    width = max([len(n) for n in blob] + [18])
    print("")
    print("Secret re-embed plan for %s:" % BLOB_MODULE)
    for name, src, old, new in changes:
        print("  %-*s  %-28s  %s -> %s"
              % (width, name, src,
                 _mask(old) if old else "<empty>",
                 _mask(new) if new else "<empty>"))
    print("  release blockers: %d -> %d"
          % (_blocker_count(current), _blocker_count(intended)))
    if not write:
        print("")
        print("Dry run only - re-run with --write to rewrite %s."
              % BLOB_MODULE)
        return 0

    with open(path, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    tree = ast.parse(text, filename=path)
    node = None
    for cand in tree.body:
        if (isinstance(cand, ast.Assign) and len(cand.targets) == 1
                and isinstance(cand.targets[0], ast.Name)
                and cand.targets[0].id == "_BLOB"):
            node = cand
    if node is None:
        raise SystemExit("could not locate the _BLOB assignment in %s" % path)
    block = ["_BLOB = {"]
    for name in blob:
        raw = encode_value(key, intended[name]) if intended[name] else ""
        block.append('    "%s": "%s",' % (name, raw))
    block.append("}")
    lines = text.splitlines(True)
    updated = ("".join(lines[:node.lineno - 1])
               + eol.join(block) + eol
               + "".join(lines[node.end_lineno:]))
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)

    check_key, check_blob = read_secret_blob(project_dir)
    check = decode_blob(check_key, check_blob)
    ok = (check_key == key
          and all(check.get(n, "") == intended[n] for n in blob))
    if not ok:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        raise SystemExit("verification failed after write - %s restored to "
                         "its previous content" % BLOB_MODULE)

    print("")
    print("Wrote %d field(s) into %s; round trip verified."
          % (len(changes), BLOB_MODULE))
    remaining = _blocker_count(intended)
    if remaining:
        print("  %d release blocker(s) remain - `scan` keeps failing the "
              "build until they are resolved." % remaining)
    else:
        print("  No release blockers remain in the blob.")
    return 0


def _needles(value, raw):
    found = []
    if value:
        found.append(("plaintext", value.encode("utf-8")))
        found.append(("utf16", value.encode("utf-16-le")))
    if raw:
        found.append(("base64", raw.encode("utf-8")))
    return found


def _mask(value):
    if len(value) <= 4:
        return "*** (%d chars)" % len(value)
    if len(value) <= 10:
        return "%s*** (%d chars)" % (value[:2], len(value))
    return "%s*** (%d chars)" % (value[:6], len(value))


def _blockers(decoded):
    out = []
    if decoded.get("DISCORD_CLIENT_SEC"):
        out.append("a Discord OAuth CLIENT SECRET ships inside the exe. It is "
                   "a server-side credential: anyone who lifts it can exchange "
                   "stolen authorisation codes as your app. PKCE is already "
                   "implemented (online.py sets pkce=True), so mark the Discord "
                   "application public, drop the secret and ROTATE the current "
                   "one - it has already been distributed.")
    if decoded.get("PRESENCE_WEBHOOK"):
        out.append("a Discord webhook URL ships inside the exe. The token in "
                   "the path lets anyone post to that channel and edit or "
                   "delete what it posted. ROTATE it and move the posting "
                   "server-side.")
    for name in ("STRIPE_MONTHLY", "STRIPE_TRIAL"):
        if "/test_" in decoded.get(name, ""):
            out.append("%s is a Stripe TEST link - checkout completes but no "
                       "money moves and no subscription activates." % name)
    return out


def scan_binary(target, project_dir, verbose=False, reveal=False):
    key, blob = read_secret_blob(project_dir)
    decoded = decode_blob(key, blob)
    with open(target, "rb") as handle:
        payload = handle.read()

    rows = []
    for name, value in decoded.items():
        hits = [label for label, needle in _needles(value, blob.get(name, ""))
                if needle in payload]
        rows.append((name, value, hits))

    width = max([len(n) for n, _, _ in rows] + [18])
    print("")
    print("Secret exposure scan: %s" % os.path.basename(target))
    print("  %.1f MiB scanned; XOR key literal present: %s"
          % (len(payload) / 1048576.0,
             "YES" if key in payload else "no"))
    print("  Nuitka compiles the source to machine code, but string literals")
    print("  survive in the data section - the XOR layer is obfuscation, not")
    print("  encryption. This is what a static scan recovers:")
    print("")
    exposed = 0
    for name, value, hits in rows:
        shown = value if reveal else _mask(value)
        if hits:
            exposed += 1
            print("  %-*s  RECOVERABLE (%s)  %s"
                  % (width, name, ",".join(sorted(set(hits))), shown))
        elif verbose:
            print("  %-*s  not found" % (width, name))
    empty = sorted(n for n, v in blob.items() if not v)

    print("")
    print("  %d of %d populated value(s) recoverable." % (exposed, len(rows)))
    if empty:
        print("  empty at build time: %s" % ", ".join(empty))

    problems = _blockers(decoded)
    if not blob.get("ROW_PEPPER"):
        problems.append("ROW_PEPPER is empty, so entitlement rows are keyed by "
                        "unsalted sha256(e-mail) in a public file.")
    if problems:
        print("")
        print("  Release blockers found by this scan:")
        for text in problems:
            print("   ! %s" % text)
    else:
        print("  No release blockers found by this scan.")
    print("")
    return len(problems)


def write_manifest(dist_dir):
    entries = []
    for root, dirs, files in os.walk(dist_dir):
        dirs.sort()
        for name in sorted(files):
            if name == MANIFEST_NAME:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, dist_dir).replace(os.sep, "/")
            entries.append((rel, _sha256_file(full), os.path.getsize(full)))
    out = os.path.join(dist_dir, MANIFEST_NAME)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# SHA-256 of every file in this folder, relative paths.\n")
        handle.write("# Regenerate: python build_tools.py manifest <folder>\n")
        for rel, digest, size in entries:
            handle.write("%s  %d  %s\n" % (digest, size, rel))
    print("Wrote %s (%d files)" % (out, len(entries)))
    return out


def verify_manifest(dist_dir):
    path = os.path.join(dist_dir, MANIFEST_NAME)
    if not os.path.exists(path):
        print("No %s in %s" % (MANIFEST_NAME, dist_dir))
        return 1
    bad = 0
    checked = 0
    listed = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("  ", 2)
            if len(parts) != 3:
                print("  MALFORMED  %s" % line[:80])
                bad += 1
                continue
            digest, _size, rel = parts
            listed.add(rel)
            full = os.path.join(dist_dir, rel.replace("/", os.sep))
            checked += 1
            if not os.path.exists(full):
                print("  MISSING  %s" % rel)
                bad += 1
            elif _sha256_file(full) != digest:
                print("  CHANGED  %s" % rel)
                bad += 1
    for root, dirs, files in os.walk(dist_dir):
        dirs[:] = [dd for dd in dirs if dd not in _RUNTIME_DIRS]
        for name in files:
            if name == MANIFEST_NAME or name in _RUNTIME_FILES:
                continue
            if name.endswith(_RUNTIME_SUFFIXES):
                continue
            rel = os.path.relpath(os.path.join(root, name),
                                  dist_dir).replace(os.sep, "/")
            if rel not in listed:
                print("  UNLISTED  %s" % rel)
                bad += 1
    print("Verified %d file(s), %d problem(s)." % (checked, bad))
    return 1 if bad else 0


def pin_dll(dll_path):
    digest = _sha256_file(dll_path)
    out = dll_path + ".sha256"
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("%s  %s\n" % (digest, os.path.basename(dll_path)))
    print("Pinned %s -> %s" % (os.path.basename(dll_path), digest))
    return out


def sign(dist_dir, exe_name):
    subject = os.environ.get("SNAPPYAI_CERT_SUBJECT", "").strip()
    pfx = os.environ.get("SNAPPYAI_CERT_PFX", "").strip()
    if not subject and not pfx:
        print("Signing skipped: set SNAPPYAI_CERT_SUBJECT or SNAPPYAI_CERT_PFX.")
        return 0
    exe = os.path.join(dist_dir, exe_name)
    if not os.path.exists(exe):
        print("Signing skipped: %s not found" % exe)
        return 1
    args = ["signtool", "sign", "/fd", "SHA256", "/td", "SHA256",
            "/tr", os.environ.get(
                "SNAPPYAI_TIMESTAMP_URL",
                "http://timestamp.digicert.com")]
    if pfx:
        args += ["/f", pfx]
        password = os.environ.get("SNAPPYAI_CERT_PASSWORD", "")
        if password:
            args += ["/p", password]
    else:
        args += ["/sha1" if len(subject) == 40 else "/n", subject]
    args.append(exe)
    shown = ["****" if a == password else a for a in args] if pfx and password \
        else list(args)
    print("Running: %s" % " ".join(shown))
    import subprocess
    try:
        rc = subprocess.call(args)
    except OSError as exc:
        print("signtool could not be started (%r)." % (exc,))
        print("It ships with the Windows SDK: 'Windows SDK Signing Tools'.")
        return 1
    if rc == 0:
        print("Signed %s" % exe)
    else:
        print("signtool exited %d - the build is UNSIGNED." % rc)
    return rc


def hpose_bundle_args(project_dir):
    models = os.path.join(project_dir, "models")
    args = [
        "--add-data", "%s%smodels" % (models, os.pathsep),
        "--hidden-import", "cv2",
        "--hidden-import", "onnxruntime",
    ]
    return args


def check_hpose(project_dir):
    missing = []
    wanted = [
        "hpose_bridge.py",
        "hpose_analyzer.py",
        "hpose_detector.py",
        os.path.join("models", "yolo11n-pose.onnx"),
        os.path.join("models", "face_detection_yunet_2023mar.onnx"),
        os.path.join("models", "hpose_bundle.npz"),
    ]
    for rel in wanted:
        path = os.path.join(project_dir, rel)
        if not os.path.isfile(path):
            missing.append(rel)
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="report recoverable build secrets")
    p.add_argument("target")
    p.add_argument("--project", default=os.path.dirname(
        os.path.abspath(__file__)))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--reveal", action="store_true",
                   help="print recovered values in full (default: masked, so "
                        "the build log stays safe to paste)")

    p = sub.add_parser("encode", help="re-embed secrets into %s from the "
                                      "SNAPPYAI_* environment" % BLOB_MODULE)
    p.add_argument("--project", default=os.path.dirname(
        os.path.abspath(__file__)))
    p.add_argument("--clear", action="append", default=[], metavar="FIELD",
                   help="empty this blob field (repeatable), e.g. "
                        "--clear DISCORD_CLIENT_SEC")
    p.add_argument("--write", action="store_true",
                   help="rewrite the file (default is a dry run)")

    p = sub.add_parser("manifest", help="write SHA256SUMS.txt for a folder")
    p.add_argument("dist")

    p = sub.add_parser("verify", help="check a folder against SHA256SUMS.txt")
    p.add_argument("dist")

    p = sub.add_parser("pin", help="write a <dll>.sha256 pin")
    p.add_argument("dll")

    p = sub.add_parser("sign", help="Authenticode-sign the exe if configured")
    p.add_argument("dist")
    p.add_argument("--exe", default="SnappyAI.exe")

    p = sub.add_parser("hpose", help="check and package the hpose files")
    p.add_argument("--project", default=os.path.dirname(
        os.path.abspath(__file__)))
    p.add_argument("--args", action="store_true",
                   help="print the PyInstaller arguments and stop")

    args = parser.parse_args(argv)
    if args.command == "scan":
        blockers = scan_binary(args.target, args.project,
                               args.verbose, args.reveal)
        return 1 if blockers else 0
    if args.command == "encode":
        return encode_blob(args.project, tuple(args.clear), args.write)
    if args.command == "manifest":
        write_manifest(args.dist)
        return 0
    if args.command == "verify":
        return verify_manifest(args.dist)
    if args.command == "pin":
        pin_dll(args.dll)
        return 0
    if args.command == "sign":
        return 1 if sign(args.dist, args.exe) else 0
    if args.command == "hpose":
        missing = check_hpose(args.project)
        if args.args:
            for arg in hpose_bundle_args(args.project):
                print(arg)
            return 0
        if missing:
            print("hpose: %d file(s) missing from the project:"
                  % (len(missing),))
            for rel in missing:
                print("  %s" % (rel,))
            return 1
        print("hpose: every file is inside the project.")
        print("  modules: hpose_*.py (bridge: hpose_bridge.py)")
        print("  models:  models/")
        print("  heads:   models/hpose_bundle.npz")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
