#!/usr/bin/env python3
"""Unit tests for the lockfile-discovery and in-place merge logic in update_spm.py.

Pure standard library, no Swift toolchain or network needed — these cover the
part of the Action that broke real builds: writing to *every* Package.resolved
Xcode reads (project + each workspace) while preserving each file's format and
never clobbering pins that belong only to a workspace's local packages.

Run: python3 test/test_merge.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import update_spm as u  # noqa: E402


def _pin(identity, version, location=None):
    return {
        "identity": identity,
        "kind": "remoteSourceControl",
        "location": location or f"https://github.com/example/{identity}.git",
        "state": {"revision": "0" * 40, "version": version},
    }


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(u._dump_resolved(data))


def test_dump_matches_swiftpm_format():
    # Space before the colon, 2-space indent, sorted keys, trailing newline.
    out = u._dump_resolved({"version": 2, "pins": []})
    assert out == '{\n  "pins" : [],\n  "version" : 2\n}\n', repr(out)


def test_find_targets_discovers_project_and_workspace_not_local_pkg():
    with tempfile.TemporaryDirectory() as d:
        proj = os.path.join(d, "MyApp.xcodeproj")
        proj_lock = os.path.join(proj, "project.xcworkspace", "xcshareddata", "swiftpm", "Package.resolved")
        ws_lock = os.path.join(d, "MyApp.xcworkspace", "xcshareddata", "swiftpm", "Package.resolved")
        local_lock = os.path.join(d, "LocalPkg", "Package.resolved")
        for p in (proj_lock, ws_lock, local_lock):
            _write(p, {"version": 2, "pins": []})

        targets = u.find_resolved_targets(proj, d)
        reals = {os.path.realpath(t["path"]) for t in targets}

        assert targets[0]["kind"] == "project"
        assert os.path.realpath(proj_lock) in reals
        assert os.path.realpath(ws_lock) in reals, "sibling .xcworkspace lockfile must be a target"
        assert os.path.realpath(local_lock) not in reals, "a local package lockfile must NOT be a target"
        # The .xcodeproj's *internal* project.xcworkspace must be counted once, as 'project'.
        assert [t["kind"] for t in targets].count("project") == 1
        assert len(targets) == len(reals), "targets must be de-duplicated"


def test_merge_noop_is_byte_identical():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "Package.resolved")
        _write(path, {"version": 3, "originHash": "keepme",
                      "pins": [_pin("alpha", "1.0.0"), _pin("beta", "2.0.0")]})
        original = open(path, encoding="utf-8").read()
        by_ident = {p["identity"]: p for p in json.loads(original)["pins"]}

        data, changes = u.plan_merge(path, by_ident)
        assert changes == []
        assert u._dump_resolved(data) == original, "no-op merge must not alter a single byte"


def test_merge_updates_values_preserves_schema_and_foreign_pins():
    with tempfile.TemporaryDirectory() as d:
        # v2 workspace file: has a pin ('local-only-dep') the resolution never sees.
        path = os.path.join(d, "Package.resolved")
        _write(path, {"version": 2, "pins": [
            _pin("remote-dep", "1.0.0"),
            _pin("local-only-dep", "3.2.1"),  # local-package dep, not in synthetic manifest
        ]})
        foreign_before = [p for p in json.loads(open(path).read())["pins"]
                          if p["identity"] == "local-only-dep"][0]

        resolution = {"remote-dep": _pin("remote-dep", "9.9.9")}  # note: no 'local-only-dep'
        data, changes = u.plan_merge(path, resolution)

        assert data["version"] == 2, "v2 must stay v2"
        assert "originHash" not in data, "must not inject originHash into a v2 file"
        assert [c[0] for c in changes] == ["remote-dep"]
        pins = {p["identity"]: p for p in data["pins"]}
        assert pins["remote-dep"]["state"]["version"] == "9.9.9"
        assert pins["local-only-dep"] == foreign_before, "workspace-only pin must be untouched"
        assert len(data["pins"]) == 2, "no pin added or removed"


def test_merge_preserves_v3_origin_hash():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "Package.resolved")
        _write(path, {"version": 3, "originHash": "abc123", "pins": [_pin("alpha", "1.0.0")]})
        data, changes = u.plan_merge(path, {"alpha": _pin("alpha", "1.1.0")})
        assert data["version"] == 3
        assert data["originHash"] == "abc123", "originHash must be preserved (requirements unchanged)"
        assert changes == [("alpha", "1.0.0", "1.1.0")]


def test_merge_adds_new_pin_sorted():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "Package.resolved")
        _write(path, {"version": 2, "pins": [_pin("mmm", "1.0.0")]})
        resolution = {"mmm": _pin("mmm", "1.0.0"), "aaa": _pin("aaa", "5.0.0")}
        data, changes = u.plan_merge(path, resolution)
        idents = [p["identity"] for p in data["pins"]]
        assert idents == sorted(idents) == ["aaa", "mmm"], "pins must stay identity-sorted"
        assert ("aaa", None, "5.0.0") in changes


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
