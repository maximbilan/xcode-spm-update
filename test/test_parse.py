#!/usr/bin/env python3
"""Unit tests for reading package requirements out of an Xcode project, in both
project file formats: the classic project.pbxproj and the JSON project.xcproj
that Xcode 27.2+ writes.

The Kinds fixtures are the same project in both formats: Kinds.xcodeproj is a
hand-written pbxproj with one remote package per requirement kind (plus a
pre-release range and a local package), and KindsJSON.xcodeproj is what
`xcodebuild -convert-project "Xcode Project"` (Xcode 27.2) wrote from it,
unedited. So these tests check the parser against real Xcode output, not
against a reading of the spec.

Pure standard library; no Swift toolchain or network needed.
Run: python3 test/test_parse.py
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import update_spm as u  # noqa: E402

FIXTURES = os.path.join(ROOT, "test", "fixture")

# Every requirement kind, as the synthetic Package.swift should express it.
EXPECTED_KINDS = [
    ("branch", "https://github.com/example/branch", 'branch: "develop"'),
    ("exact", "https://github.com/example/exact", 'exact: "3.4.5"'),
    ("major", "https://github.com/example/major", 'from: "1.2.3"'),
    ("minor", "https://github.com/example/minor.git", '.upToNextMinor(from: "2.3.4")'),
    ("prerelease", "https://github.com/example/prerelease.git", '"1.0.0-beta.1"..<"2.0.0"'),
    ("range", "https://github.com/example/range", '"4.5.6"..<"5.0.0"'),
    ("revision", "https://github.com/example/revision",
     'revision: "0123456789abcdef0123456789abcdef01234567"'),
]


def _deps(project_dir):
    return u.parse_dependencies(u.locate_project_file(os.path.join(FIXTURES, project_dir)))


def _as_tuples(deps):
    return [(d["name"], d["url"], d["requirement_swift"]) for d in deps]


def _xcproj_deps(packages_json):
    """Parse a project.xcproj whose only content is the given packages array."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, u.XCPROJ)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{\n  "packages": %s,\n}\n' % packages_json)
        return u.parse_dependencies(path)


def _raises_value_error(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    raise AssertionError("expected ValueError")


def test_pbxproj_every_requirement_kind():
    assert _as_tuples(_deps("Kinds.xcodeproj")) == EXPECTED_KINDS


def test_xcproj_every_requirement_kind():
    assert _as_tuples(_deps("KindsJSON.xcodeproj")) == EXPECTED_KINDS


def test_both_formats_yield_identical_manifests():
    pbx = _deps("Kinds.xcodeproj")
    xc = _deps("KindsJSON.xcodeproj")
    assert pbx == xc
    assert u.render_manifest(pbx) == u.render_manifest(xc)


def test_xcproj_sample_fixture():
    # The fixture the macOS self-test resolves for real.
    assert _as_tuples(_deps("SampleJSON.xcodeproj")) == [
        ("swift-collections", "https://github.com/apple/swift-collections", 'from: "1.0.0"'),
        ("swift-log", "https://github.com/apple/swift-log", 'from: "1.0.0"'),
    ]


def test_relaxed_json_comments_and_trailing_commas():
    text = """{
      // line comment, with a trailing comma inside it: ,]
      "url": "https://example.com/a//b",  /* block
         comment spanning lines */
      "tricky": "keeps ,] and /* and // inside strings",
      "escaped": "quote \\" then // still a string",
      "list": [1, 2, /* c */ ],
      "nested": { "a": [ ], },
    }"""
    assert u._loads_relaxed_json(text) == {
        "url": "https://example.com/a//b",
        "tricky": "keeps ,] and /* and // inside strings",
        "escaped": 'quote " then // still a string',
        "list": [1, 2],
        "nested": {"a": []},
    }


def test_relaxed_json_error_keeps_line_numbers():
    text = '{\n  /* one\n     two */\n  "a": 1,\n  "b": nope,\n}\n'
    try:
        u._loads_relaxed_json(text)
    except json.JSONDecodeError as e:
        assert e.lineno == 5, e
    else:
        raise AssertionError("expected JSONDecodeError")


def test_xcproj_skips_local_and_requires_a_requirement():
    assert _xcproj_deps('[{"kind": "local", "path": "Pkg"}]') == []
    msg = _raises_value_error(lambda: _xcproj_deps(
        '[{"kind": "remote", "repository": "https://github.com/example/naked"}]'))
    assert "naked" in msg, msg


def test_xcproj_rejects_ranges_xcode_rejects():
    # Xcode 27.2 refuses to open all of these (checked with xcodebuild), so fail
    # loudly rather than resolve something the project doesn't actually say.
    for version in ('{"version-range": "1.0.0...2.0.0"}',
                    '{"version-range-min": "1.0.0"}',
                    '{"something-new": "1.0.0"}'):
        _raises_value_error(lambda: _xcproj_deps(
            '[{"kind": "remote", "repository": "https://x/y", "version": %s}]' % version))


def test_locate_project_file():
    with tempfile.TemporaryDirectory() as d:
        _raises_value_error(lambda: u.locate_project_file(d))
        open(os.path.join(d, u.XCPROJ), "w").close()
        assert u.locate_project_file(d) == os.path.join(d, u.XCPROJ)
        open(os.path.join(d, u.PBXPROJ), "w").close()
        msg = _raises_value_error(lambda: u.locate_project_file(d))
        assert "both" in msg, msg
        os.remove(os.path.join(d, u.XCPROJ))
        assert u.locate_project_file(d) == os.path.join(d, u.PBXPROJ)


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
