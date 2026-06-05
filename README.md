# Update Xcode Swift Packages

A composite GitHub Action that bumps an Xcode project's Swift Package Manager
pins on Xcode 16+, where `xcodebuild -resolvePackageDependencies` stopped
reliably updating versions.

## Why

`xcodebuild -resolvePackageDependencies` is the documented way to refresh SPM
pins from the command line. In Xcode 16+ that path frequently no-ops even when
newer versions are available within the declared constraints — which is why
most "update SPM" CI jobs (and the only off-the-shelf action that wraps
xcodebuild) silently stopped doing their job.

This action routes around the problem without reimplementing version solving:

1. Parses `XCRemoteSwiftPackageReference` entries (repo URL + version
   requirement) straight out of `project.pbxproj`.
2. Writes a throwaway `Package.swift` that declares those same dependencies.
3. Runs `xcrun swift package update` on the synthetic manifest — the real
   PubGrub solver resolves the full transitive graph.
4. Copies the resulting `Package.resolved` back into the Xcode project's
   internal workspace.

The action is pure Python standard library — no `pip install`, no Node
dependencies — and it must be run on a macOS runner because it shells out to
the Swift toolchain.

## Usage

```yaml
jobs:
  update:
    runs-on: macos-15   # any macos-* image works
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4

      - id: update
        uses: maximbilan/xcode-spm-update@v1
        with:
          project: MyApp.xcodeproj      # optional; autodetects if omitted
          xcode-version: '16.4'         # optional; pin to your team's Xcode

      - name: Open PR
        if: steps.update.outputs.dependencies-changed == 'true'
        uses: peter-evans/create-pull-request@v6
        with:
          branch: deps/spm-update
          title: 'Update Swift package dependencies'
          commit-message: 'chore: update Swift package dependencies'
          body: '${{ steps.update.outputs.changed-count }} package(s) changed.'
```

A complete working example lives at
[`examples/update-spm.yml`](examples/update-spm.yml).

## Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `project` | no | `''` | Path to the `.xcodeproj`. When empty, autodetects a single `.xcodeproj` under `working-directory`. |
| `working-directory` | no | `.` | Directory the action runs in (used for autodetection and as the script's cwd). |
| `xcode-version` | no | `''` | Xcode version to select via `xcode-select` before resolving (e.g. `16.4`). Leave empty to use the runner default. |
| `fail-when-outdated` | no | `false` | When `true`, the action exits non-zero if any pin would change. Useful as a CI guard on a PR branch. |
| `dry-run` | no | `false` | When `true`, resolve into a temp directory and report changes without writing `Package.resolved` back. |

## Outputs

| Name | Description |
|------|-------------|
| `dependencies-changed` | `'true'` if at least one pin would change, otherwise `'false'`. |
| `changed-count` | Number of pins that changed, as an integer string. |

## Caveats

- **Project-internal Package.resolved.** This action writes
  `Package.resolved` to
  `<MyApp.xcodeproj>/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`
  — the path Xcode reads when SPM is managed inside the `.xcodeproj` itself.
  Projects that drive SPM from a standalone `.xcworkspace` (the resolved file
  lives under the workspace, not the project) will need their workflow
  adjusted, since this action does not write to that location.
- **Unsupported reference kinds.** Only `XCRemoteSwiftPackageReference` is
  parsed. Local packages (`XCLocalSwiftPackageReference`) and registry
  dependencies (`.package(id:)`) are skipped — they don't appear in the
  remote-reference section. Mirror those dependencies in your team's normal
  workflow.
- **Match the Xcode used locally.** Run the action with the same Xcode version
  your team uses (the `xcode-version` input). Package.resolved has a format
  version and an `originHash` field that changes across Xcode releases; if the
  CI-resolved file disagrees with what your local Xcode expects, Xcode will
  silently re-resolve on first open and clobber the PR.

## How it works internally

`update_spm.py` is the entire implementation. From the repo root it does:

1. Reads `project.pbxproj` and slices out the
   `Begin XCRemoteSwiftPackageReference section` ... `End ...` block.
2. For each reference: pulls `repositoryURL` and the `requirement = { ... }`
   dict, then maps the pbxproj `kind` to a Swift `.package(url:...)` argument:
   `upToNextMajorVersion` → `from:`, `upToNextMinorVersion` →
   `.upToNextMinor(from:)`, `exactVersion` → `exact:`, `versionRange` →
   `"a"..<"b"`, `branch:` and `revision:` map literally.
3. Writes the synthetic `Package.swift` to a temp directory and runs
   `xcrun swift package update --package-path <tmp>`.
4. Diffs the new `Package.resolved` against the project's current one and
   reports which identities changed.
5. Unless `--dry-run` or `--fail-when-outdated`, copies the new file in place.

The script is invoked from the action via `$GITHUB_ACTION_PATH/update_spm.py`,
so the action is fully self-contained — consumers do not need to vendor the
script into their own repo.

## License

[MIT](LICENSE)
