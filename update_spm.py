#!/usr/bin/env python3
"""
update_spm.py — Update an Xcode project's Swift Package dependencies from the CLI.

Why this exists: `xcodebuild -resolvePackageDependencies` (the usual CLI path, and
what the only off-the-shelf Action wraps) stopped reliably bumping versions in
Xcode 16+. This routes around it: it reads the dependency *requirements* straight
out of project.pbxproj, writes a throwaway Package.swift, lets the real `swift`
toolchain do the resolving (PubGrub solver, transitive graph, the works), then
copies the freshly resolved Package.resolved back into the Xcode project.

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
import shutil
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


def parse_dependencies(pbxproj_path):
    """Return a list of dicts: {name, url, requirement_swift} parsed from pbxproj."""
    text = open(pbxproj_path, encoding="utf-8").read()
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
    deps.sort(key=lambda d: d["name"].lower())
    return deps


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


def find_resolved_path(project_path):
    """Locate the Package.resolved Xcode reads for this .xcodeproj."""
    candidate = os.path.join(
        project_path, "project.xcworkspace", "xcshareddata", "swiftpm", "Package.resolved"
    )
    return candidate


def summarize_diff(old_path, new_path):
    def load(p):
        if not os.path.exists(p):
            return {}
        data = json.load(open(p))
        return {pin["identity"]: pin["state"].get("version") or pin["state"].get("revision", "")[:7]
                for pin in data.get("pins", [])}
    old, new = load(old_path), load(new_path)
    changed = []
    for ident in sorted(set(old) | set(new)):
        a, b = old.get(ident), new.get(ident)
        if a != b:
            changed.append((ident, a, b))
    return changed


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

    pbxproj = os.path.join(project, "project.pbxproj")
    if not os.path.exists(pbxproj):
        print(f"error: {pbxproj} not found", file=sys.stderr)
        return 1

    print(f"Project: {project}")
    deps = parse_dependencies(pbxproj)
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
    target = find_resolved_path(project)
    changed = summarize_diff(target, new_resolved)
    changed_count = len(changed)
    emit_outputs(changed_count)

    if not changed:
        print("\nAll dependencies already up to date. No changes.")
        return 0

    print(f"\n{changed_count} package(s) changed:")
    for ident, old, new in changed:
        print(f"  {ident:<28} {old or '(new)'} -> {new}")

    if args.fail_when_outdated:
        print("\n--fail-when-outdated set: exiting with code 3 because pins are stale.",
              file=sys.stderr)
        return 3

    if args.dry_run:
        print(f"\n[dry-run] Updated Package.resolved left at: {new_resolved}")
        return 0

    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(new_resolved, target)
    print(f"\nWrote updated pins to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
