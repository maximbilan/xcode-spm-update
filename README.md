# Update Xcode Swift Packages

A GitHub Action that **actually updates Swift Package Manager dependencies in an `.xcodeproj`** — even on Xcode 16+, where the official tooling silently stopped doing it.

Think of it as `npm update` / `bundle update` / `go get -u ./...` for Swift packages pinned inside an Xcode project. Dependabot and Renovate don't cover this case; the only off-the-shelf action that wrapped `xcodebuild` no longer works reliably either.

## The problem in one minute

If you ship an iOS or macOS app, you probably have something like this on a schedule:

```bash
xcodebuild -resolvePackageDependencies -project MyApp.xcodeproj
```

That command is Apple's documented way to refresh your `Package.resolved` (the lockfile that pins every Swift package to an exact version). The intent: a CI job runs this weekly, sees that `Alamofire 5.9.0 → 5.10.0` is now allowed by your version constraints, writes the new pin, and opens a PR.

**On Xcode 16+, it frequently does nothing.** The command exits successfully, prints no errors, and leaves `Package.resolved` untouched — even when newer versions are clearly available within your declared `from:` / `upToNextMajor` ranges. Your weekly "update dependencies" job keeps reporting "no changes" while your pins quietly fall months behind.

This Action is a drop-in replacement for that step.

## Do I need this?

You probably want this Action if **all** of the following are true:

- [x] You maintain an iOS, macOS, watchOS, tvOS, or visionOS app
- [x] Your Swift package dependencies are managed **inside an `.xcodeproj`** (not a standalone `Package.swift`)
- [x] You're on Xcode 16 or newer
- [x] You want CI to open PRs that bump those dependencies — and your existing automation has gone quiet

If your project is a pure Swift package (top-level `Package.swift`), you don't need this — `swift package update` already works.

## How the fix works

Rather than fight `xcodebuild`, the Action sidesteps it:

```
.xcodeproj/project.pbxproj
        │
        │  parse XCRemoteSwiftPackageReference entries
        ▼
synthetic Package.swift  ──►  xcrun swift package update  ──►  fresh Package.resolved
                                                                       │
                                                                       │  copy back into
                                                                       ▼
                              .xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved
```

In other words: read the package URLs and version requirements straight out of `project.pbxproj`, hand them to the real Swift Package Manager solver (which still works fine), and drop the resulting lockfile back into the place Xcode reads it from.

Implementation is a single pure-Python script using only the standard library — no `pip install`, no Node dependencies — and it must run on a macOS runner because it shells out to the Swift toolchain.

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

A complete working example lives at [`examples/update-spm.yml`](examples/update-spm.yml).

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

- **Project-internal `Package.resolved` only.** This Action writes to
  `<MyApp.xcodeproj>/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`
  — the path Xcode reads when SPM is managed inside the `.xcodeproj` itself.
  Projects that drive SPM from a standalone `.xcworkspace` (where the resolved
  file lives under the workspace, not the project) will need their workflow
  adjusted, since this Action does not write to that location.
- **Unsupported reference kinds.** Only `XCRemoteSwiftPackageReference` is
  parsed. Local packages (`XCLocalSwiftPackageReference`) and registry
  dependencies (`.package(id:)`) are skipped — they don't appear in the
  remote-reference section. Mirror those dependencies in your team's normal
  workflow.
- **Match the Xcode used locally.** Run the Action with the same Xcode version
  your team uses (the `xcode-version` input). `Package.resolved` has a format
  version and an `originHash` field that changes across Xcode releases; if the
  CI-resolved file disagrees with what your local Xcode expects, Xcode will
  silently re-resolve on first open and clobber the PR.

<details>
<summary><strong>How it works internally</strong> (click to expand)</summary>

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

The script is invoked from the Action via `$GITHUB_ACTION_PATH/update_spm.py`,
so the Action is fully self-contained — consumers do not need to vendor the
script into their own repo.

</details>

## License

[MIT](LICENSE)
