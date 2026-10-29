# MotionSync

MotionSync is a desktop application for reviewing 1080 Motion runs alongside
their video recordings.

## Install

Download the latest release for your computer from
[GitHub Releases](https://github.com/mmni13/MotionSync/releases/latest).

### Windows x64

Download and run `MotionSync-Setup-<version>-windows-x64.exe`. The installer is
per-user and does not require administrator privileges. It does not remove run
data, settings, or caches when upgrading or uninstalling.

### macOS Apple Silicon

Download and open `MotionSync-<version>-macos-arm64.dmg`, then drag MotionSync
to Applications. The initial release is unsigned. If Gatekeeper blocks it,
Control-click MotionSync in Applications, choose **Open**, and confirm the
prompt. Automatic Contemplas catalog discovery is Windows-only; on macOS, use
the selected-folder video scan or attach a recording manually.

## Updates

MotionSync checks GitHub Releases at startup and offers to open the release
page when a newer version is available. Close MotionSync before installing the
new version. Existing run data, API settings, and caches remain in the per-user
application data directory.

## Build and release

Runtime dependencies are pinned in `requirements.txt`; build tools are pinned
in `requirements-build.txt`. Tag a commit with the same version as
`viewer/version.py`, for example `v1.0.0`, and push the tag. GitHub Actions
builds the Windows installer and Apple Silicon DMG, creates SHA-256 files, and
publishes them to a GitHub Release.

The macOS build is unsigned until a Developer ID signing certificate and
notarization credentials are configured. Do not distribute unsigned builds
without the Gatekeeper instructions above.