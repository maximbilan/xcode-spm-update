#!/usr/bin/env python3
"""
update_spm.py — Update an Xcode project's Swift Package dependencies from the CLI.

Why this exists: `xcodebuild -resolvePackageDependencies` (the usual CLI path, and
what the only off-the-shelf Action wraps) stopped reliably bumping versions in
Xcode 16+. This routes around it: it reads the dependency *requirements* straight
out of the project file — the classic property-list project.pbxproj, or the JSON
project.xcproj that Xcode 27.2+ writes — then writes a throwaway Package.swift,
lets the real `swift` toolchain do the resolving (PubGrub solver, transitive graph, the works), then
merges the freshly resolved pins back into *every* Package.resolved Xcode reads
for the project — the one embedded in the .xcodeproj AND the one inside any sibling
.xcworkspace, since `xcodebuild -workspace` (CI/release builds) reads the workspace
copy and would otherwise never see the update. Each lockfile is patched in place,
values-only, preserving its own schema version and formatting.

It does NOT reimplement version solving. SPM does that. We only do plumbing.

Usage:
    python3 update_spm.py [--project path/to/MyApp.xcodeproj] [--search-dir .]
                          [--dry-run] [--fail-when-outdated]

Exit codes:
    0  success (Package.resolved updated, or already up to date)
    1  usage / parse error
    2  `swift package update` failed
    3  --fail-when-outdated and dependencies were out of date
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

# Each remote package reference: grab its name, repositoryURL, and the *inner*
# `requirement = { ... }` block (parsing that block on its own avoids the nested
# braces tripping up the key/value scan).
REMOTE_RE = re.compile(
    r'XCRemoteSwiftPackageReference "(?P<name>[^"]+)" \*/ = \{'
    r'.*?repositoryURL\s*=\s*"(?P<url>[^"]+)";'
    r'.*?requirement\s*=\s*\{(?P<req>.*?)\};',
    re.S,
)
KV_RE = re.compile(r'(\w+)\s*=\s*"?([^";{}]+?)"?\s*;')

# The project definition file inside a .xcodeproj bundle. Xcode 27.2+ defaults to
# the JSON format; a bundle holds exactly one of the two.
PBXPROJ = "project.pbxproj"
XCPROJ = "project.xcproj"

# project.xcproj is JSON plus two relaxations Xcode both writes and reads: trailing
# commas, and // or /* */ comments. Each regex matches a whole string literal first,
# so comment markers or commas *inside* strings (URLs, shell scripts) are left alone.
_JSON_STRING = r'"(?:\\.|[^"\\])*"'
_JSON_COMMENT_RE = re.compile(_JSON_STRING + r'|//[^\n]*|/\*.*?\*/', re.S)
_JSON_TRAILING_COMMA_RE = re.compile(f'({_JSON_STRING})|,(\\s*[}}\\]])')

# How project.xcproj spells each version requirement inside a package's "version"
# object, in the order Xcode's own decoder checks them (apple/xcode-project-format,
# SwiftPackageVersionConstraint.swift), mapped onto the pbxproj requirement dict
# that _requirement_to_swift already understands. Ranges are handled separately.
_XCPROJ_REQUIREMENTS = (
    ("revision", "revision", "revision"),
    ("branch", "branch", "branch"),
    ("version", "exactVersion", "version"),
    ("up-to-next-minor-version", "upToNextMinorVersion", "minimumVersion"),
    ("up-to-next-major-version", "upToNextMajorVersion", "minimumVersion"),
)


def locate_project_file(project):
    """Return the path of the .xcodeproj bundle's project definition file.

    Raises ValueError if the bundle has neither file, or has both: Xcode itself
    refuses to open a bundle with both, so we don't guess which one is current.
    """
    present = [os.path.join(project, f) for f in (PBXPROJ, XCPROJ)
               if os.path.exists(os.path.join(project, f))]
    if not present:
        raise ValueError(f"neither {PBXPROJ} nor {XCPROJ} found in {project}")
    if len(present) > 1:
        raise ValueError(f"{project} contains both {PBXPROJ} and {XCPROJ}; Xcode refuses "
                         "to open a project like this, so remove the obsolete one")
    return present[0]


def parse_dependencies(project_file):
    """Return a list of dicts: {name, url, requirement_swift}, sorted by name, for
    every remote package in a project.pbxproj or a JSON project.xcproj."""
    if project_file.endswith(".xcproj"):
        deps = _parse_xcproj(project_file)
    else:
        deps = _parse_pbxproj(project_file)
    deps.sort(key=lambda d: d["name"].lower())
    return deps


def _parse_pbxproj(path):
    text = open(path, encoding="utf-8").read()
    try:
        section = text.split("Begin XCRemoteSwiftPackageReference section")[1]
        section = section.split("End XCRemoteSwiftPackageReference section")[0]
    except IndexError:
        return []  # no remote packages

    deps = []
    for m in REMOTE_RE.finditer(section):
        req = dict(KV_RE.findall(m.group("req")))
        deps.append({
            "name": m.group("name"),
            "url": m.group("url"),
            "requirement_swift": _requirement_to_swift(req, m.group("name")),
        })
    return deps


def _parse_xcproj(path):
    data = _loads_relaxed_json(open(path, encoding="utf-8").read())
    deps = []
    for pkg in data.get("packages", []):
        if pkg.get("kind") != "remote":
            continue  # local packages resolve from disk; nothing to update
        url = pkg.get("repository")
        if not url:
            raise ValueError(f"remote package without a repository URL: {pkg!r}")
        name = _package_name_from_url(url)
        req = _xcproj_requirement(pkg.get("version"), name)
        deps.append({"name": name, "url": url,
                     "requirement_swift": _requirement_to_swift(req, name)})
    return deps


def _loads_relaxed_json(text):
    """json.loads for project.xcproj: drop comments and trailing commas first.
    Removed characters become spaces, so a JSONDecodeError still reports the
    right line and column."""
    def blank_comment(m):
        s = m.group(0)
        return s if s.startswith('"') else re.sub(r"[^\n]", " ", s)

    text = _JSON_COMMENT_RE.sub(blank_comment, text)
    text = _JSON_TRAILING_COMMA_RE.sub(lambda m: m.group(1) or " " + m.group(2), text)
    return json.loads(text)


def _package_name_from_url(url):
    """Display name for a package: the repository's last path component without
    `.git` (what the pbxproj format shows in its reference comments)."""
    last = url.rstrip("/").rsplit("/", 1)[-1]
    return last[:-len(".git")] if last.endswith(".git") else last


def _xcproj_requirement(version, name):
    """Translate a project.xcproj package "version" object into the pbxproj-style
    requirement dict, so both formats share one path to Package.swift."""
    if not isinstance(version, dict):
        raise ValueError(f"no version requirement for dependency '{name}'")
    for key, kind, field in _XCPROJ_REQUIREMENTS:
        if key in version:
            return {"kind": kind, field: version[key]}
    if "version-range" in version:
        # Compact form, written when both bounds are plain numbers: "1.0.0..<2.0.0".
        value = version["version-range"]
        lo, sep, hi = value.partition("..<")
        if not sep or "..<" in hi:
            raise ValueError(f"unexpected version-range '{value}' for dependency '{name}'")
        return {"kind": "versionRange", "minimumVersion": lo, "maximumVersion": hi}
    if "version-range-min" in version and "version-range-max" in version:
        # Long form, written when a bound isn't a plain number (e.g. a pre-release).
        return {"kind": "versionRange", "minimumVersion": version["version-range-min"],
                "maximumVersion": version["version-range-max"]}
    raise ValueError(f"unsupported version requirement {sorted(version)} for dependency '{name}'")


def _requirement_to_swift(req, name):
    """Translate a pbxproj requirement dict into a Package.swift argument string."""
    kind = req.get("kind", "")
    if kind == "upToNextMajorVersion":
        return f'from: "{req["minimumVersion"]}"'
    if kind == "upToNextMinorVersion":
        return f'.upToNextMinor(from: "{req["minimumVersion"]}")'
    if kind == "exactVersion":
        return f'exact: "{req.get("version", req.get("minimumVersion"))}"'
    if kind == "versionRange":
        return f'"{req["minimumVersion"]}"..<"{req["maximumVersion"]}"'
    if kind == "branch":
        return f'branch: "{req["branch"]}"'
    if kind == "revision":
        return f'revision: "{req["revision"]}"'
    raise ValueError(f"Unsupported requirement kind '{kind}' for dependency '{name}'")


def render_manifest(deps, tools_version="5.10"):
    lines = [
        f"// swift-tools-version:{tools_version}",
        "// AUTO-GENERATED throwaway manifest used only to resolve dependency versions.",
        "import PackageDescription",
        "",
        "let package = Package(",
        '    name: "SPMResolver",',
        "    dependencies: [",
    ]
    for d in deps:
        lines.append(f'        .package(url: "{d["url"]}", {d["requirement_swift"]}),')
    lines += [
        "    ],",
        "    targets: []",
        ")",
        "",
    ]
    return "\n".join(lines)


def _dump_resolved(data):
    """Serialize a Package.resolved dict exactly the way SwiftPM does: 2-space
    indent, sorted keys, a space *before* each colon, trailing newline. Byte-identical
    to Xcode's own output, so an unchanged file round-trips with zero diff and a
    values-only merge produces a values-only diff (no formatting churn). Matches
    Foundation's JSONEncoder, which emits raw UTF-8 (ensure_ascii=False) so a
    non-ASCII package name or URL isn't rewritten as \\uXXXX escapes."""
    return json.dumps(data, indent=2, separators=(",", " : "),
                      sort_keys=True, ensure_ascii=False) + "\n"


def _state_summary(state):
    """Short human-readable form of a pin's resolved state, for logging."""
    if not state:
        return "(new)"
    return state.get("version") or state.get("branch") or (state.get("revision") or "")[:7] or "(?)"


def find_resolved_targets(project, search_dir):
    """Every Package.resolved that Xcode reads for this project.

    Xcode keeps a lockfile per *container*: one embedded in the .xcodeproj, and a
    separate one inside each .xcworkspace. `xcodebuild -workspace` (what CI/release
    builds usually run) resolves against the *workspace* lockfile, not the .xcodeproj
    one. If we only touch the project file, the workspace file goes stale and real
    builds never see the update — so we update every lockfile we can find.

    Returns a list of {"path", "kind"} dicts (kind is "project" or "workspace"),
    with the .xcodeproj-embedded lockfile first.
    """
    targets = []
    seen = set()

    def add(path, kind):
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            targets.append({"path": path, "kind": kind})

    # The lockfile embedded in the .xcodeproj bundle.
    add(os.path.join(project, "project.xcworkspace", "xcshareddata", "swiftpm",
                     "Package.resolved"), "project")

    # Sibling .xcworkspace lockfiles, looked for next to the project and under
    # search-dir. (The .xcodeproj's *internal* project.xcworkspace lives inside the
    # bundle, so a `*.xcworkspace` glob against these dirs never matches it — no
    # double-counting.)
    for d in {os.path.dirname(os.path.abspath(project)) or ".", os.path.abspath(search_dir)}:
        for ws in sorted(glob.glob(os.path.join(d, "*.xcworkspace"))):
            add(os.path.join(ws, "xcshareddata", "swiftpm", "Package.resolved"), "workspace")

    return targets


def plan_merge(target_path, new_pins_by_ident):
    """Compute an in-place, format-preserving update of an existing Package.resolved.

    Only pins whose identity appears in the fresh resolution are updated; pins unique
    to this file are left byte-for-byte untouched and no pin is ever removed. This
    matters for workspace lockfiles, whose graph is a superset of the synthetic
    manifest's — it also pins dependencies pulled in by local packages that the
    manifest never sees; blowing those away would break resolution. The file's schema
    version and originHash are preserved: only resolved values change, not the version
    requirements, so an existing originHash stays valid.

    Returns (data, changes) where `data` is the mutated dict ready to write and
    `changes` is a list of (identity, old_summary, new_summary).
    """
    data = json.loads(open(target_path, encoding="utf-8").read())
    pins = data.get("pins", [])
    existing = {p.get("identity") for p in pins}
    changes = []

    for pin in pins:
        newp = new_pins_by_ident.get(pin.get("identity"))
        if newp is None:
            continue  # not in our resolution (e.g. a local package's dependency)
        if pin.get("state") != newp.get("state"):
            changes.append((pin["identity"], _state_summary(pin.get("state")),
                            _state_summary(newp.get("state"))))
            pin["state"] = newp["state"]

    for ident, newp in new_pins_by_ident.items():
        if ident not in existing:
            pins.append({k: newp[k] for k in ("identity", "kind", "location", "state")
                         if k in newp})
            changes.append((ident, None, _state_summary(newp.get("state"))))

    # SwiftPM writes pins sorted by identity; keep that so newly-added pins land in a
    # stable spot (a no-op when nothing was added, preserving byte-for-byte output).
    data["pins"] = sorted(pins, key=lambda p: p.get("identity", ""))
    return data, changes


def autodetect_project(search_dir):
    """Find a single .xcodeproj under search_dir. Returns the path or None."""
    matches = sorted(glob.glob(os.path.join(search_dir, "*.xcodeproj")))
    if not matches:
        matches = sorted(glob.glob(os.path.join(search_dir, "**", "*.xcodeproj"), recursive=True))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"error: multiple .xcodeproj found under {search_dir}; pass --project to disambiguate:",
              file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
    return None


def emit_outputs(changed_count):
    """Write GitHub Actions outputs if $GITHUB_OUTPUT is set."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"dependencies-changed={'true' if changed_count > 0 else 'false'}\n")
        fh.write(f"changed-count={changed_count}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="",
                    help="Path to the .xcodeproj (autodetected from --search-dir if omitted)")
    ap.add_argument("--search-dir", default=".",
                    help="Directory to search for a .xcodeproj when --project is not given (default: .)")
    ap.add_argument("--tools-version", default="5.10",
                    help="swift-tools-version for the synthetic manifest. Determines Package.resolved format "
                         "(5.9 -> v2, 5.10+ -> v3 with originHash). Default 5.10 matches Xcode 15.3+.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Generate manifest and resolve into a temp dir, but don't overwrite the project's Package.resolved")
    ap.add_argument("--print-manifest", action="store_true", help="Print the synthetic Package.swift and exit")
    ap.add_argument("--fail-when-outdated", action="store_true",
                    help="Exit with code 3 if any dependency would be updated (useful for CI guards)")
    args = ap.parse_args()

    project = args.project or autodetect_project(args.search_dir)
    if not project:
        if not args.project:
            print(f"error: no .xcodeproj found under {args.search_dir}; pass --project",
                  file=sys.stderr)
        return 1

    try:
        project_file = locate_project_file(project)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        deps = parse_dependencies(project_file)
    except ValueError as e:  # includes json.JSONDecodeError
        print(f"error: {project_file}: {e}", file=sys.stderr)
        return 1

    print(f"Project: {project} ({os.path.basename(project_file)})")
    if not deps:
        print("No remote Swift Package dependencies found.")
        emit_outputs(0)
        return 0
    print(f"Found {len(deps)} direct dependencies:")
    for d in deps:
        print(f"  - {d['name']:<28} {d['requirement_swift']}")

    manifest = render_manifest(deps, args.tools_version)
    if args.print_manifest:
        print("\n--- synthetic Package.swift ---\n" + manifest)
        return 0

    tmp = tempfile.mkdtemp(prefix="spmresolve_")
    open(os.path.join(tmp, "Package.swift"), "w").write(manifest)

    print("\nResolving with `swift package update` ...")
    proc = subprocess.run(
        ["xcrun", "swift", "package", "update", "--package-path", tmp],
        capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print("error: swift package update failed", file=sys.stderr)
        return 2

    new_resolved = os.path.join(tmp, "Package.resolved")
    new_data = json.loads(open(new_resolved, encoding="utf-8").read())
    new_pins_by_ident = {p["identity"]: p for p in new_data.get("pins", [])}

    targets = find_resolved_targets(project, args.search_dir)

    # Build a write plan for every lockfile without touching disk yet, so --dry-run
    # and --fail-when-outdated report accurately before anything is written.
    plans = []            # list of (target_dict, data_to_write, changes)
    changed_idents = set()
    for t in targets:
        path = t["path"]
        if os.path.exists(path):
            data, changes = plan_merge(path, new_pins_by_ident)
        elif t["kind"] == "project":
            # No lockfile in the .xcodeproj yet — create it from the full resolution
            # (this container's graph is exactly the synthetic manifest's).
            data = new_data
            changes = [(ident, None, _state_summary(p.get("state")))
                       for ident, p in new_pins_by_ident.items()]
        else:
            # A workspace with no lockfile yet: skip. We can't author a complete one
            # from scratch — the workspace graph includes local-package dependencies
            # the synthetic manifest never sees, so the file would be missing pins.
            print(f"note: {path} does not exist yet; skipping "
                  "(open the workspace in Xcode once to create it, then re-run)")
            continue
        plans.append((t, data, changes))
        changed_idents.update(c[0] for c in changes)

    changed_count = len(changed_idents)
    emit_outputs(changed_count)

    if changed_count == 0:
        print("\nAll dependencies already up to date. No changes.")
        return 0

    print(f"\n{changed_count} package(s) to update across {len(plans)} lockfile(s):")
    for t, _data, changes in plans:
        if not changes:
            print(f"  {t['path']} ({t['kind']}): already current")
            continue
        print(f"  {t['path']} ({t['kind']}): {len(changes)} pin(s)")
        for ident, old, new in changes:
            print(f"    {ident:<28} {old or '(new)'} -> {new}")

    if args.fail_when_outdated:
        print("\n--fail-when-outdated set: exiting with code 3 because pins are stale.",
              file=sys.stderr)
        return 3

    if args.dry_run:
        print(f"\n[dry-run] No files written. Fresh resolution left at: {new_resolved}")
        return 0

    for t, data, changes in plans:
        if not changes:
            continue  # already current — don't rewrite (avoids a no-op diff)
        os.makedirs(os.path.dirname(t["path"]), exist_ok=True)
        with open(t["path"], "w", encoding="utf-8") as fh:
            fh.write(_dump_resolved(data))
        print(f"\nWrote {len(changes)} updated pin(s) to {t['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
