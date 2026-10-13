# Native CLI releases and Homebrew

The native archive includes the compiled TypeScript CLI, a PyInstaller onedir
runtime, a relocatable launcher, an installer, and build metadata. Node.js 20+
is the only separately installed language runtime. Homebrew supplies Node.
The mesh daemon remains a separate installation. No models are bundled.

The public tap is `newtorob/homebrew-potluck`. Public archives use immutable
versioned URLs and contain the user-facing `DOWNLOAD.md` as their README. The
formula downloads the archives at the URLs recorded in their metadata, not from
the source repository. No workflow in `.github/workflows` builds or publishes CLI
archives; they are built by hand on each target host. Development artifacts from
dirty checkouts must not be published.

## Build on each target host

Use a reviewed release checkout and a native Python environment. Build separately
on each supported OS/architecture; do not relabel a binary from another host.

```sh
python3 scripts/build-split-engine.py --source /path/to/llama.cpp --backend metal
# On Linux choose cpu or cuda and the required CUDA compiler/architectures.
apps/desktop/python/.venv/bin/python apps/desktop/python/build.py
pnpm --dir apps/cli package:release \
  --runtime "$PWD/apps/desktop/python/dist/potluck-local" \
  --out "$PWD/release/cli" --version <version>
```

For public macOS releases, sign the runtime and its nested libraries with the
Developer ID identity and notarize the distribution before publishing. Reuse
the repository's sidecar signing procedure; verify signatures after packaging.
Do not use an ad-hoc development build as a production download. Checksums must
be calculated from the final distributed bytes.

The script emits `.tar.gz`, `.tar.gz.sha256`, and `.tar.gz.json`. Metadata records
the Git revision, whether the checkout was dirty, OS, architecture, runtime hash,
the pinned split engine identity and backend,
and archive SHA-256. Default URLs are under
`https://releases.trypotluck.ai/cli/<version>/`. `--base-url` can select another
HTTPS release directory. Building and generating a formula do not upload files.
Packaging fails if the frozen runtime lacks compatible native split executables.
See [split setup and validation](../../../docs/managed-split-models.md).

For an exported source tree on another build host, capture provenance on the
original checkout and transfer it with the source. Pass `--provenance` to the
packager; never invent a revision or clear a dirty flag:

```sh
node --input-type=module -e 'import {execFileSync as run} from "node:child_process"; console.log(JSON.stringify({source:run("git",["rev-parse","HEAD"],{encoding:"utf8"}).trim(),dirty:!!run("git",["status","--porcelain"],{encoding:"utf8"}).trim()}))' > source-metadata.json
```

Install CLI development dependencies on the build host before packaging. They
are needed to compile TypeScript and are excluded from the archive.

## Validate an archive

Verify its checksum, extract into a temporary directory, and run the included
`install.mjs --prefix <temporary-prefix>`. Verify `bin/potluck --version` and
`--help` from outside the checkout. Exercise bundled runtime discovery without
`POTLUCK_RUNTIME_BIN`. Native service validation uses isolated state and a unique
service name, then removes its test service:

```sh
POTLUCK_CLI_ENTRY=/temporary-prefix/lib/potluck/current/libexec/cli/dist/index.js \
  node apps/cli/scripts/validate-user-service.cjs
```

The driver checks coexistence, ownership handoff, authenticated app discovery,
restart, credential rotation, shutdown cleanup and preserved settings. It never
enrolls a machine or enables the gateway. Keep its report as release evidence.
Also run `pnpm --dir apps/cli test` and the Python runtime-control tests.

Validate the usage dashboard with the installed command and its bundled runtime:

```sh
python3 apps/cli/scripts/validate-usage-dashboard.py \
  --cli /temporary-prefix/bin/potluck \
  --report /new-output-directory/usage-dashboard.json
```

This standard-library-only POSIX check creates disposable state, starts the engine
on an isolated loopback port, verifies authenticated metadata and runs the actual
CLI in a pseudoterminal. It tests views, filters, themes, resize, truecolor/256-color/16-color modes,
NO_COLOR, TERM=dumb, piped snapshots, NDJSON watch, keyboard/cursor restoration
after quit/Ctrl-C/SIGTERM, and actual runtime process exit. Add `--tmux` to test
an isolated tmux server on hosts where it is installed. Recorded
usage is synthetic test metadata, not a throughput benchmark. It does not sign
in, enable sharing, install models or change services. Omit `--runtime` to verify
bundled runtime discovery; the optional override is for build debugging.

## Prepare the Homebrew tap

Generate one formula from the actual metadata for every architecture being
published. Missing binaries are not replaced with placeholder URLs or hashes.

```sh
mkdir -p release/homebrew/Formula
pnpm --dir apps/cli package:homebrew \
  "$PWD/release/homebrew/Formula/potluck.rb" \
  "$PWD/release/cli/potluck-cli-<version>-darwin-arm64.tar.gz.json" \
  "$PWD/release/cli/potluck-cli-<version>-linux-x64.tar.gz.json"
```

Host a public, formula-only `homebrew-potluck` tap and the signed/verified
archives at the metadata URLs. Do not put private source code or signing material
in the tap. Upload immutable versioned archives and checksum files first, verify
public download hashes, then publish `Formula/potluck.rb`. No CI workflow or
paid build service is required. Do not change desktop updater feeds.

Once published, the install form is `brew install <tap-owner>/potluck/potluck`.
Validate the published formula with `brew test` and `brew services start`,
`restart`, and `stop`. The formula installs no service automatically; its service
runs `potluck serve`, logs privately, and keeps account/control secrets out of
the plist. `potluck service` delegates to Homebrew.

For a local development formula test, stage the verified archive in the path
reported by `brew --cache --build-from-source <temporary-tap>/potluck`. Use an
isolated Homebrew installation or snapshot existing dependency versions first:
ordinary `brew install` may upgrade Node and its dependencies even when automatic
Homebrew updates are disabled. Remove temporary services and the test tap after
validation. The formula uses `preserve_rpath` and `skip_clean` to retain frozen
runtime bytes; verify this by comparing the installed runtime's file hashes.

Homebrew references: [Formula Cookbook](https://docs.brew.sh/Formula-Cookbook)
and [maintaining a tap](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap).

## Upgrade and rollback

Standalone installs place versions under `<prefix>/lib/potluck` and atomically
switch its `current` link. Existing versions remain. Stop the native service
before rollback, point `current` at a retained version in that directory, then
run `potluck service install` from the stable command to update executable paths.
An exact version cannot be installed twice. The installer refuses foreign
commands, unsafe install directories, and incompatible architectures.

Uninstall the service before removing binaries. Removing an installation never
requires deleting `~/.potluck`; that directory contains the user's settings,
accounts and models. Homebrew upgrades and uninstall use Homebrew's lifecycle.
