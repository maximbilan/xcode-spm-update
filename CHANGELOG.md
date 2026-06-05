# Changelog

All notable changes to this project will be documented in this file. This
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
