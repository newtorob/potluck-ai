# Security policy

## Supported versions

Security fixes are released for the latest version of the desktop app and the CLI. The app updates itself, and older versions do not receive fixes.

| Component | Supported |
| --- | --- |
| Desktop app, latest release | Yes |
| CLI, latest release | Yes |
| Older releases | No. Update to the latest release |

## Reporting a vulnerability

Please do not open a public issue or pull request for a security problem, and do not disclose it to third parties before it has been addressed.

1. **Email us.** Send a report to [rob@trypotluck.ai](mailto:rob@trypotluck.ai) with "Security" in the subject line. Include:
   - what you found and where (component, file, endpoint or build),
   - how to reproduce it,
   - what an attacker could do with it,
   - the version or commit you tested.
2. **We confirm receipt.** We will tell you that we received the report and what we plan to do about it.
3. **We work on a fix.** We may ask you for more detail while we investigate, and we will keep you informed.
4. **Coordinated disclosure.** Once a fix is released, we are glad to coordinate publication with you and credit you if you want that.

We do not offer monetary bounties.

## Scope

- The desktop app, the engine, the mesh sidecar and the CLI in this repository.
- The coordinator, cloud API and relay in `services/`, and the hosted instances of them at `trypotluck.ai`.
- Release artifacts published at `trypotluck.ai` and through the Homebrew tap.

Testing against the hosted services must not degrade them for other people or touch data that is not yours. Use your own account and your own machines.

## Background

- [docs/security/threat-model.md](docs/security/threat-model.md) describes what the system protects against, what it does not, and which protections are implemented or planned.
- [docs/security/release-key.md](docs/security/release-key.md) describes how contributor builds are verified.
- [docs/security/peer-quality.md](docs/security/peer-quality.md) describes how the network checks the machines that answer requests.
