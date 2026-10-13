# Contributor release attestation

A contributor machine that serves inference for other members presents the hash of the build it is running. The coordinator checks that hash against a table of registered releases before it accepts the machine. This doc describes how that check works, how a release is registered and verified, and what it does and does not prove.

## How it works

1. A build writes `contributor-release.json` into the engine bundle. The file holds `binary_hash`, a SHA-256 over the bundle contents taken before code signing (`apps/desktop/python/potluck_local/contributor/release.py`), plus the version and build time.
2. `scripts/dev/sign-contributor-release.py` signs that hash with an Ed25519 release key. It signs the ASCII hex form of the SHA-256, and the signature is encoded as base64url. The key is identified by a fingerprint: the first 16 hex characters of the SHA-256 of the raw public key.
3. The signed hash is registered as one row in the coordinator's `contributor_releases` table: `binary_hash` (primary key), `version`, `signed_at`, `signer_key` (the fingerprint) and `notes`. The `emit-sql` and `emit-sql-stamp` subcommands print the `INSERT` for a row, with the signer fingerprint and the signature as SQL comments. `python -m coordinator.tools.register_release` (in `services/coordinator`) registers a hash from the command line and lists registered releases.
4. When a contributor engine connects to `/peers/connect`, it answers the coordinator's nonce challenge with its device key and sends its `binary_hash` in the attestation payload. The coordinator looks the hash up in `contributor_releases`.

An engine started from a source checkout, with no stamp, presents `unreleased-dev`. `POTLUCK_BINARY_HASH` overrides the value for test machines.

## The `REQUIRE_REGISTERED_RELEASE` setting

| Value | Behavior |
|---|---|
| `true` | A peer whose `binary_hash` is not in `contributor_releases` is rejected at the handshake. |
| `false` (the code default) | The peer is accepted, the event is logged as `peer_release_unregistered`, and the hash is recorded on the peer's heartbeat row. Development and tests use this. |

The coordinator's preflight check (`services/coordinator/coordinator/services/preflight.py`) reports a warning when the setting is off. A related setting, `REQUIRE_REGISTERED_KEYS`, restricts attestation to device keys that are already registered to a member.

## Subcommands of the signing script

| Command | What it does |
|---|---|
| `keygen` | Generates an Ed25519 keypair. The private key is written to disk with mode `0600` and never printed. |
| `info` | Prints the public key fingerprint. |
| `sign <artifact>` | Prints the artifact's SHA-256 and the signature. |
| `verify <artifact> <signature>` | Checks a signature against the artifact and the public key. Prints `VALID` or `INVALID`. |
| `emit-sql <artifact>` | Signs a single file and prints the `contributor_releases` `INSERT`. |
| `emit-sql-stamp <stamp>` | The same, for the hash in a bundle's `contributor-release.json`. |

## How a release is verified

- **By the coordinator**, at every contributor handshake, by the table lookup described above.
- **Offline**, by anyone who holds the release public key: `sign-contributor-release.py verify <artifact> <signature>` recomputes the SHA-256 and checks the Ed25519 signature over it. `sign-contributor-release.py info` prints the fingerprint to compare with the `signer_key` of a registered row.

## What it proves

The check is an allowlist of known builds. It tells the coordinator that the hash a contributor presents belongs to a build that was registered. It does not prove the contributor is running that binary, because a peer can present any registered hash it has seen. It also does not prove the contributor's machine is uncompromised. The coordinator treats the hash as one layer of defense. Verification of what a contributor returns relies on the other mechanisms in [the threat model](threat-model.md) (canary probes, reputation and quality scoring). Hardware attestation is not part of this scheme.

The coordinator does not check the Ed25519 signature at the handshake. It relies on the registration step: only hashes that were registered through the signing and registration tooling are in the table.

## Key custody

How the release key is stored and protected is kept private. The private key is never in the repository.

## Reporting a problem

If you believe a registered release is malicious, or that the release key has been exposed, write to rob@trypotluck.ai.
