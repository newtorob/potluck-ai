# Desktop entry and account access

This contract covers entry, desktop chat routing, and the sign-in boundary. Other desktop workflows retain their existing contracts and are outside this focused audit. Visual owners are recorded in [DESIGN.md](DESIGN.md); user instructions are in [Getting started](docs/install.md).

## Product rules and evidence

| Rule | Source |
| --- | --- |
| Local chat, model management and local memory can be used without an account. | `docs/install.md`, `AuthGate`, `localUse` |
| Pairing, trusted circles, network controls, administration and new inference-gateway setup require account access. | `App.tsx`, `AccountRequired`, `GatewayPanel`; backend authorization remains authoritative. |
| A local entry choice does not enable contribution or stop existing background services. | `ConnectStep`, `GatewayPanel`, `docs/install.md` |
| Email verification requires explicit account confirmation before committing a session. | `useAuth`, `useDeepLinkAuth`, `SignInConfirmDialog` |
| Local history and model installations survive later sign-in. | `localUse.test.tsx`, local sidecar persistence |

Component paths below are relative to `apps/desktop/frontend/src/`.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Form | `components/auth/MagicLinkForm.tsx` | `hooks/useAuth.ts` and passwordless confirmation flow | Entry form without input autofocus; dedicated sign-in form with autofocus | AuthGate and localUse tests; browser error/keyboard checks |
| Account entry | `components/auth/AuthGate.tsx`, `SignInWall.tsx` | Explicit local choice or confirmed/cached account | Account lookup, local entry, signed-in and cached offline access | `components/auth/AuthGate.test.tsx` |
| Account-backed feature | `components/auth/AccountRequired.tsx` | `stores/authStore.ts` | Full feature or in-shell sign-in prompt | `lib/localUse.test.tsx` |
| Desktop routing | `views/ChatView.tsx`, `stores/chatStore.ts` | Explicit scope and local-use preference, checked again at dispatch | Local, owned machines, trusted circle, cooperative scope subject to availability | Routing, recovery and gateway tests |
| Durable local preference | `stores/uiPrefsStore.ts`, `lib/durablePrefs.ts` | Latest explicit choice wins over in-flight hydration | Restored true/false state, new local or remote choice | `lib/localUse.test.tsx` |

## State and recovery

- Entry offers **Use on this machine** while an account lookup is pending. Choosing it selects Local and preserves history, models and other settings.
- A late account response or settings response must not change that choice. Effective chat scope and the status display agree with the dispatch guard.
- A previously loaded distributed model is preserved. Local chat explains how to load a model on this machine; a signed-in member may explicitly choose **Use this model** to accept its existing remote configuration.
- A failed runtime check prevents the local chat request and gives a recovery action. It never silently falls back to another machine.
- Signing in keeps local work. Invalid or pending sign-in does not mount account-backed controls. Existing cached offline accounts retain their established behavior.
- Account-required prompts keep shell navigation and a path back to local chat. The inference-gateway panel still permits disabling a previously configured service while signed out.
- Sign-in errors stay inline, retain the entered email, expose an alert, and leave local entry available. Duplicate submissions are disabled while pending.

## Verification boundary

Use component/store tests for delayed hydration, auth transitions, preserved history, actual dispatch guards and gateway setup restrictions. Use the isolated actual-component browser preview for keyboard focus, sign-in failure, local entry, remote-model guidance, and narrow light/dark layouts. Mocked UI previews are not live-inference or native-installer certification.

The existing universal minimum-version gate and server authorization policies remain in force. Local entry is a desktop-chat choice, not a promise that every app activity or previously configured service is offline.
