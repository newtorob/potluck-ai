# Potluck AI: macOS Release Runbook

This document covers the steps to produce a signed, notarized macOS distribution.
Ad-hoc local builds work with no credentials. Setting the signing and
notarization environment variables below upgrades the same build to a
production build.

---

## Prerequisites

### Apple Developer Program membership
- Enroll at https://developer.apple.com/programs/enroll/
- The Team ID is the 10-character string shown on your membership page. This
  document writes it as `<TEAM_ID>`.

### Certificates in the keychain
- Download and install the "Developer ID Application" certificate from
  https://developer.apple.com/account/resources/certificates/list
- electron-builder auto-discovers it from the macOS Keychain via the `CSC_LINK` /
  `CSC_KEY_PASSWORD` env vars, or from the keychain directly when those vars are absent.
- For CI, export the certificate as a `.p12` and set:
  ```
  CSC_LINK=<base64-encoded .p12>
  CSC_KEY_PASSWORD=<.p12 export password>
  ```

### App-specific password for notarization
Apple's notarytool requires an app-specific password, never your Apple ID login password.

1. Sign in at https://appleid.apple.com
2. Security → App-Specific Passwords → Generate Password
3. Label it "potluck-notarize" (or similar)
4. Save the generated password. Apple shows it only once.

### Optional: store the credentials in the keychain for notarytool
For manual `notarytool` commands (log retrieval, status checks), store the
credentials once as a keychain profile. `store-credentials` prompts for the
password, so it never appears on a command line or in shell history:

```bash
xcrun notarytool store-credentials "potluck-notarize" \
  --apple-id "you@example.com" \
  --team-id "<TEAM_ID>"
```

---

## Required environment variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `CODESIGN_IDENTITY` | Signing identity for sidecars. Default: `-` (ad-hoc). | `Developer ID Application: <Company Name> (<TEAM_ID>)` |
| `APPLE_ID` | Your Apple Developer Apple ID (email) | `you@example.com` |
| `APPLE_APP_SPECIFIC_PASSWORD` | App-specific password from appleid.apple.com | `<app-specific-password>` |
| `APPLE_TEAM_ID` | Your 10-char Developer Team ID | `<TEAM_ID>` |
| `CSC_LINK` | (CI only) Base64-encoded .p12 certificate | see above |
| `CSC_KEY_PASSWORD` | (CI only) .p12 export password | |

The `afterSign` hook (`scripts/notarize.js`) skips notarization, and the build
still succeeds, when any of the three `APPLE_*` variables is missing.

---

## Production release command

Export the variables so every step of the build sees them. A `VAR=value cmd1 && cmd2`
prefix applies only to `cmd1`. Read the password from your secret store instead of
typing it into the shell:

```bash
export CODESIGN_IDENTITY="Developer ID Application: <Company Name> (<TEAM_ID>)"
export APPLE_ID="you@example.com"
export APPLE_APP_SPECIFIC_PASSWORD="<app-specific-password>"
export APPLE_TEAM_ID="<TEAM_ID>"
```

Then run, from the repo root:

```bash
make installer-mac
```

`make installer-mac` builds the Python and Go sidecars (`make installer-bundle`)
and then runs `pnpm run build:mac` in `apps/desktop/electron/`. If the sidecars
are already built, run the electron steps directly from `apps/desktop/electron/`:

```bash
pnpm run prepack && pnpm run build:mac
```

What this does, in order:
1. `prepack:frontend`: Vite production build of the React frontend
2. `prepack:python`: signs all Mach-O files in `python/dist/potluck-local/` with `CODESIGN_IDENTITY`, then copies to `resources/python/`
3. `prepack:mcp`: same for `python/dist/potluck-mcp/`
4. `prepack:mesh`: builds the Go mesh sidecar, signs it, and copies it to `resources/mesh/`
5. `prepack:mesh-helper`: builds and signs the Swift `mesh-helper` into `resources/mesh-helper/`
6. `prepack:build-info`: stamps the git sha and build time into `build-info.json`
7. `build:mac`: electron-builder packages the .app, signs it (re-signs the sidecar with hardened runtime), runs the `afterSign` notarize hook, produces `.dmg` and `.zip` artifacts, then `scripts/hash-release.js` writes `SHA256SUMS`

The notarize hook submits the `.app` to Apple, waits for approval (usually 1-5 minutes), then staples the ticket.

---

## Ad-hoc (local) build

Leave the Apple env vars unset:

```bash
pnpm run prepack && pnpm run build:mac
```

Sidecars will be ad-hoc signed. The app will build and run on the same machine
(and any machine with Gatekeeper disabled), but Gatekeeper will block it on other
machines. That is the expected state for local development and testing.

---

## Verifying the output

After the build completes, verify from `apps/desktop/electron/`:

```bash
# Strict codesign check: must pass with zero errors
codesign --verify --strict --verbose=4 "out/mac-arm64/Potluck AI.app"

# Gatekeeper assessment: must report "accepted"
spctl --assess --type execute --verbose "out/mac-arm64/Potluck AI.app"

# Check the signing identity on the .app itself
codesign -dvvv "out/mac-arm64/Potluck AI.app" 2>&1 | grep -E "TeamIdentifier|Signature|flags="

# Check the sidecar is also properly signed
codesign -dvvv "out/mac-arm64/Potluck AI.app/Contents/Resources/python/potluck-local/potluck-local" 2>&1 | grep -E "TeamIdentifier|Signature|flags="

# Verify notarization staple is present (production builds only)
xcrun stapler validate "out/Potluck AI-<version>-arm64.dmg"
```

A correctly signed production build will show:
- `valid on disk` and `satisfies its Designated Requirement` from `codesign --verify`
- `accepted` from `spctl --assess`
- `TeamIdentifier=<TEAM_ID>` in the codesign output
- `The staple and validate action worked!` from `xcrun stapler validate`

---

## If notarization fails

The `notarize.js` afterSign hook prints the submission ID before throwing. Use it to
fetch the full log from Apple, with the keychain profile stored above:

```bash
xcrun notarytool log <submission-id> --keychain-profile "potluck-notarize"
```

### Common failure causes

**"The signature of the binary is invalid"**
A binary inside the bundle has an invalid or missing signature. The sidecar
pre-signing step (`sign-sidecars.sh`) should prevent this. Re-run the prepack
scripts and check that all `.so`/`.dylib` files in `_internal/` are signed
before the build runs.

**"The executable does not have the hardened runtime enabled"**
electron-builder signs with `hardenedRuntime: true`. If you're seeing this, the
signing identity may have been overridden. Verify `CSC_LINK` is pointing to a
Developer ID Application cert (not Development or Distribution).

**"com.apple.security.cs.disable-library-validation is present"**
This entitlement is not in `entitlements.plist` and must not be added back.
If notarization starts rejecting builds for this reason, the root cause is
unsigned `.dylib`s. Run `sign-sidecars.sh` and find which binary is missing a
signature.

**Notarization takes a long time**
Apple's service occasionally queues submissions. Check a submission with
`xcrun notarytool info <submission-id> --keychain-profile "potluck-notarize"`,
or list recent ones with `xcrun notarytool history --keychain-profile "potluck-notarize"`.
If it still shows "In Progress" after an hour, resubmit.

**"Unable to find provisioning profile"**
Potluck AI is a Developer ID (outside Mac App Store) build. It must not have a
provisioning profile. Verify that `mac.provisioningProfile` is not set in
`package.json`.

---

## Notes

- `disable-library-validation` is not in `entitlements.plist`. Do not add it.
  Its purpose is to allow unsigned dylibs to load; since `sign-sidecars.sh`
  signs every Mach-O before packaging, it is not needed, and adding it would
  widen the attack surface.

- The two sidecars (`potluck-local` and `potluck-mcp`) each contain many
  Mach-O files in their `_internal/` directories, so the sign step takes a
  while for each one.

- electron-builder re-signs the entire `.app` bundle (including the sidecar
  executables) after copying from `resources/`. The pre-signing step by
  `sign-sidecars.sh` ensures the inner `.dylib`s are individually signed with
  the correct entitlements before that re-sign pass. Without it, the inner
  libraries would be unsigned inside the sealed bundle.

- Windows and Linux packaging is covered in
  [`packaging/README.md`](../../../packaging/README.md).
