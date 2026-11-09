# Changelog

All notable changes to this project will be documented in this file. This
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.3] - 2026-11-09

### Added
- Support for the JSON project file format (`<Name>.xcodeproj/project.xcproj`)
  that Xcode 27.2 and later writes by default. Previously the Action failed
  on these projects with `error: <Name>.xcodeproj/project.pbxproj not found`.
  The format is detected automatically: remote packages and every version
  requirement kind are read from the `packages` array, including the
  `version-range-min`/`version-range-max` form Xcode uses for pre-release
  bounds. Lockfile locations are unchanged.

### Changed
- A `.xcodeproj` that contains both `project.pbxproj` and `project.xcproj` is
  now rejected with a clear error, matching Xcode, which refuses to open it.
- An unsupported or missing version requirement now prints a one-line
  `error:` and exits 1, instead of a Python traceback.

## [1.0.2] - 2026-07-23

### Fixed
- Update **every** `Package.resolved` Xcode reads for the project, not just the
  one embedded in the `.xcodeproj`. Previously, projects built through a
  `.xcworkspace` (via `xcodebuild -workspace`) resolved against the workspace's
  own lockfile, which the Action never touched — so dependency bumps landed in
  the project lockfile but never reached real builds, and the two files drifted.
  The Action now discovers the project lockfile and each sibling
  `.xcworkspace/xcshareddata/swiftpm/Package.resolved`.

### Changed
- Lockfiles are now patched **in place** (values only) instead of being
  overwritten wholesale: each file keeps its own schema version (`v2`/`v3`),
  `originHash`, formatting, and any pins that belong only to that container
  (e.g. dependencies of a workspace's local packages). This yields minimal,
  values-only diffs with no formatting churn.
- `changed-count` now reports the number of distinct pins changed across all
  lockfiles; `dependencies-changed` is `true` if any lockfile would change.

## [1.0.0] - 2026-06-05

Initial public release as a Marketplace composite action.

### Added
- `action.yml` composite action wrapping `update_spm.py`.
- Inputs: `project`, `working-directory`, `xcode-version`,
  `fail-when-outdated`, `dry-run`.
- Outputs: `dependencies-changed`, `changed-count`.
- `--search-dir` autodetection so `project` can be omitted when there is
  exactly one `.xcodeproj` under the working directory.
- `--fail-when-outdated` flag for using the action as a CI guard.
- `$GITHUB_OUTPUT` emission directly from the Python script.
- Self-test workflow at `.github/workflows/test.yml` that runs the action
  against a checked-in pbxproj fixture in dry-run mode.
- Example consumer workflow at `examples/update-spm.yml`.
