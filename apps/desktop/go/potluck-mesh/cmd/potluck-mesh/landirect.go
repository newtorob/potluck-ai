package main

import (
	"net"
	"os"
	"sync"
	"time"
)

// Phase 2 — LAN direct-path discovery (VERIFIED trial/flip state machine).
//
// Phase 0/1 (committed) made every node discover its own private-LAN endpoints
// and carry each peer's LAN candidates in the roster (RosterPeer.LocalEndpoints).
// Nothing READ those candidates to make a routing decision. This phase does:
// for a same-LAN peer that is currently relayed via DERP, it runs a VERIFIED
// trial that moves the peer onto a direct LAN connection — and falls back to
// DERP on any failure so a peer is never stranded on a dead direct endpoint.
//
// SAFETY: the entire mechanism is gated behind POTLUCK_LAN_DIRECT (default OFF).
// When off, lanDirectEnabled() returns false and none of the trial/verify code
// runs — the peer stays on the existing DERP path exactly as before. DERP is the
// floor at all times: a peer only LEAVES DERP after a fresh handshake is
// observed on the LAN endpoint, and any timeout rolls it straight back to DERP.

// lanDirectEnabledEnv is read once at startup (see lanDirectEnabled). Kept as a
// package var so tests can drive the pure state machine without touching env.
var lanDirectEnabledEnv = os.Getenv("POTLUCK_LAN_DIRECT") != ""

// lanDirectEnabled reports whether the Phase-2 LAN direct-path trial is on.
// The flag is read ONCE at process start (var initialiser above) so toggling it
// mid-run has no effect — behaviour is fixed for the lifetime of the process.
func lanDirectEnabled() bool { return lanDirectEnabledEnv }

// routingMode is the per-peer managed routing state.
//
//   - modeDERP:       peer is relayed via the DERP relay (the default / floor).
//   - modeLANTrial:   the peer's WG session was CLEARED (RemovePeer) and the peer
//     re-added pinned to a same-LAN endpoint with a short persistent keepalive,
//     forcing wireguard-go to re-handshake on the LAN endpoint; we are waiting to
//     VERIFY that fresh handshake before declaring success. DERP is OFF during
//     the trial, so a dead LAN path means the peer is unreachable until the trial
//     deadline rolls it back to DERP (RemovePeer + re-add for DERP + flip).
//   - modeLANDirect:  a fresh handshake was observed on the LAN endpoint; the
//     peer is confirmed reachable directly and stays off the relay.
//   - modePunchTrial: the WAN twin of modeLANTrial — driven by a coordinator
//     punch_coord (not a same-LAN co-subnet match), pinned to the peer's
//     STUN-discovered real endpoint instead of a LAN ip:port. Same force-rekey
//     mechanism, same verify/timeout state machine (see runPunchTrialLoop).
//   - modeDirect:     the WAN twin of modeLANDirect — a fresh handshake was
//     observed on the punched endpoint; the peer is confirmed reachable
//     directly (cross-network) and stays off the relay.
//
// All four non-DERP modes are "managed": signal-driven raw-endpoint updates
// (roster_update.Updated, punch_coord) MUST NOT overwrite the WG endpoint while
// a peer is in any of them, or they would clobber either the synthetic DERP
// endpoint (modeDERP) or the direct endpoint we are trialling/using.
type routingMode int

const (
	modeDERP routingMode = iota
	modeLANTrial
	modeLANDirect
	modePunchTrial
	modeDirect
)

func (m routingMode) String() string {
	switch m {
	case modeLANTrial:
		return "lan-trial"
	case modeLANDirect:
		return "lan-direct"
	case modePunchTrial:
		return "punch-trial"
	case modeDirect:
		return "direct"
	default:
		return "derp"
	}
}

// lanTrialKeepalive is the persistent-keepalive (seconds) we pin onto the peer
// for the DURATION of a LAN trial. This is the linchpin of the force-rekey fix.
//
// Why a SHORT keepalive is required: simply re-pointing a peer's endpoint
// (UpdatePeerEndpoint) does NOT make wireguard-go re-handshake — the existing
// DERP-established session stays valid for ~2 min (REKEY_AFTER_TIME), so on an
// idle mesh no new handshake ever fires on the LAN endpoint and the trial always
// times out. By REMOVING the peer (clearing the session) and re-ADDING it pinned
// to the LAN endpoint WITH a short persistent keepalive, we force WG to bring up
// a session promptly: a keepalive packet needs a live session, so WG initiates a
// handshake to the LAN endpoint right away even with zero application traffic.
// That handshake is what evaluateLANTrial verifies. 5s is short enough to fire
// well inside the trial window yet not absurdly chatty for the few seconds the
// trial runs. On VERIFIED we re-add with the standard keepalive so a confirmed
// link is not keepalive-spammed.
const lanTrialKeepalive = 5

// lanDirectKeepalive is the persistent-keepalive (seconds) re-applied to a peer
// once its LAN trial is VERIFIED. Matches the standard mesh keepalive (25s, see
// rosterPeerToTunnelPeer) so a confirmed direct link behaves exactly like any
// other peer and we stop the 5s trial-keepalive spam.
const lanDirectKeepalive = 25

// lanTrialDuration is how long a LAN-trial peer is given to produce a fresh
// handshake before we give up and roll back to DERP.
//
// Trade-off (justified): during the trial DERP is OFF, so a DEAD LAN path means
// the peer is unreachable for the whole trial window. The trial pins a short
// persistent keepalive (lanTrialKeepalive = 5s) on the freshly re-added peer,
// which makes wireguard-go initiate a handshake to the LAN endpoint promptly
// even on an idle mesh. On a live LAN the handshake round-trips in ~1 RTT
// (sub-ms to low-ms) once the keepalive fires, so a healthy path verifies within
// a few seconds. We pick 12s: comfortably longer than the 5s keepalive trigger
// plus one WG handshake retry (~5s, REKEY_TIMEOUT) and clock skew/jitter between
// the two nodes, while still bounding a dead LAN candidate to ~12s of
// (recoverable) unreachability before the peer is restored to the working relay.
const lanTrialDuration = 12 * time.Second

// lanTrialBackoff is how long a peer whose LAN trial FAILED is kept on DERP
// before another trial is allowed. Prevents thrashing: a LAN candidate that is
// advertised but unreachable (firewalled WG port, asymmetric route, stale
// address) would otherwise be retried every reachability tick, repeatedly
// knocking the peer off the working relay for lanTrialDuration each time.
const lanTrialBackoff = 5 * time.Minute

// lanVerifyInterval is the cadence of the dedicated fast ticker that checks
// trial peers for a fresh handshake (or an expired deadline). It is much faster
// than the 30s reachability poll so a successful trial is confirmed — and a
// failed one rolled back — within a couple of seconds rather than tens of
// seconds. Scoped to trial peers only; no-op when none are in trial.
const lanVerifyInterval = 2 * time.Second

// modeTracker is a tiny thread-safe map of base64-pubkey → routingMode shared
// between the reachability/verify loop (the writer, which owns the trial state
// machine) and the roster/punch handler closures (readers, which use it to
// decide whether a raw-endpoint update is safe).
//
// IMPORTANT — off-by-default invariant: the tracker records ONLY the LAN modes
// (lan-trial / lan-direct). The plain DERP / unknown case is NOT stored, so a
// peer absent from the map reads back as modeDERP. This is what keeps the
// feature byte-for-byte inert when POTLUCK_LAN_DIRECT is off: the writer only
// ever stores a LAN mode from inside the flag-gated trial code, so with the flag
// off the map stays empty, isManaged() is always false, and the guard
// `isManaged() || dBind.IsPeerDERPRouted()` collapses to exactly the legacy
// `dBind.IsPeerDERPRouted()` check.
//
// The guard must skip raw-endpoint overwrites for LAN modes because, during a
// trial / once direct, dBind.IsPeerDERPRouted reports FALSE (DERP is off for
// that peer) — yet the WG endpoint is the LAN ip:port we must not clobber.
type modeTracker struct {
	mu sync.RWMutex
	m  map[string]routingMode
}

func newModeTracker() *modeTracker {
	return &modeTracker{m: make(map[string]routingMode)}
}

// set records the peer's mode. modeDERP is stored as a DELETE (the absence of an
// entry IS modeDERP) so the map never holds derp entries — preserving the
// off-by-default invariant above.
func (t *modeTracker) set(pubkeyBase64 string, mode routingMode) {
	t.mu.Lock()
	if mode == modeDERP {
		delete(t.m, pubkeyBase64)
	} else {
		t.m[pubkeyBase64] = mode
	}
	t.mu.Unlock()
}

// get returns the recorded mode, defaulting to modeDERP for unknown peers.
func (t *modeTracker) get(pubkeyBase64 string) routingMode {
	t.mu.RLock()
	defer t.mu.RUnlock()
	if m, ok := t.m[pubkeyBase64]; ok {
		return m
	}
	return modeDERP
}

// remove drops a peer's mode entry (called when a peer leaves the roster).
func (t *modeTracker) remove(pubkeyBase64 string) {
	t.mu.Lock()
	delete(t.m, pubkeyBase64)
	t.mu.Unlock()
}

// isManaged reports whether the peer is in ANY managed direct-path mode
// (lan-trial, lan-direct, punch-trial, or direct) — the case where
// dBind.IsPeerDERPRouted would (correctly) report false yet a raw-endpoint
// overwrite must still be blocked. modeDERP / unknown returns false so the
// caller falls through to the dBind DERP check; that is what makes the guard
// identical to legacy behaviour when neither LAN direct nor a punch trial is
// active for the peer.
func (t *modeTracker) isManaged(pubkeyBase64 string) bool {
	switch t.get(pubkeyBase64) {
	case modeLANTrial, modeLANDirect, modePunchTrial, modeDirect:
		return true
	default:
		return false
	}
}

// sameLANEndpoint returns the first peer LAN "ip:port" candidate whose IP lies
// in the SAME subnet as one of my local interface IPNets, judged using MY
// interface mask (co-subnet test). This is stricter than "both look like
// 192.168.x": two hosts on 192.168.1.0/24 and 192.168.2.0/24 share a /16 prefix
// but are NOT co-subnet under a /24 mask, and this correctly rejects them.
//
// It also rejects a peer whose advertised LAN IP equals one of MY OWN interface
// IPs (the hairpin / same-address case that can arise behind a single NAT where
// both report the gateway-assigned address), since routing a packet at our own
// address is never a usable direct path.
//
// Returns ("", false) when no candidate co-subnets with any local interface.
func sameLANEndpoint(myLocalIPNets []net.IPNet, peerLocalEndpoints []string) (string, bool) {
	for _, ep := range peerLocalEndpoints {
		host, _, err := net.SplitHostPort(ep)
		if err != nil {
			continue
		}
		peerIP := net.ParseIP(host).To4()
		if peerIP == nil {
			continue
		}
		for _, n := range myLocalIPNets {
			myIP := n.IP.To4()
			if myIP == nil || len(n.Mask) == 0 {
				continue
			}
			// Reject the hairpin/self case: the peer cannot be reached at our
			// own address.
			if myIP.Equal(peerIP) {
				continue
			}
			// Co-subnet test under MY mask: both IPs map to the same network.
			if myIP.Mask(n.Mask).Equal(peerIP.Mask(n.Mask)) {
				return ep, true
			}
		}
	}
	return "", false
}

// ---------------------------------------------------------------------------
// Pure-ish routing-mode transition helpers.
//
// These take the current per-peer trial fields + observed facts and return the
// NEXT mode (plus, where relevant, the side-effect inputs). They perform NO I/O
// so they can be unit-tested as a state machine. The reachability/verify loop
// owns the actual WG/DERP side effects (flipToDERP / UpdatePeerEndpoint).
// ---------------------------------------------------------------------------

// shouldStartLANTrial reports whether a peer that is currently in modeDERP
// should begin a LAN trial. Conditions:
//   - LAN direct is enabled,
//   - the peer is in modeDERP (we never trial out of trial/direct),
//   - a same-LAN candidate exists,
//   - the peer is not in trial backoff (now < backoffUntil means suppressed).
func shouldStartLANTrial(enabled bool, mode routingMode, hasSameLANEp bool, backoffUntil, now time.Time) bool {
	if !enabled {
		return false
	}
	if mode != modeDERP {
		return false
	}
	if !hasSameLANEp {
		return false
	}
	if now.Before(backoffUntil) {
		return false
	}
	return true
}

// lanTrialOutcome is the verdict for a peer currently in modeLANTrial.
type lanTrialOutcome int

const (
	// trialPending: no fresh handshake yet and the deadline has not passed —
	// keep waiting.
	trialPending lanTrialOutcome = iota
	// trialVerified: a handshake NEWER than the pre-trial snapshot was observed
	// on the LAN endpoint — promote to modeLANDirect.
	trialVerified
	// trialTimedOut: the deadline passed with no fresh handshake — roll back to
	// modeDERP and enter backoff.
	trialTimedOut
)

// evaluateLANTrial decides the outcome of an in-progress LAN trial.
//
// A trial is VERIFIED iff the peer's current last-handshake time is strictly
// newer than the snapshot taken at trial start (preTrialHandshake) AND is itself
// fresh (within handshakeFreshWindow). The "newer than snapshot" test is what
// proves the handshake completed over the LAN endpoint we just set, not a stale
// one left over from before the trial. The freshness test guards the edge case
// where the snapshot was the zero time and any non-zero historical handshake
// would otherwise look "newer".
//
// If not yet verified, the trial times out once now >= trialDeadline.
//
// Reused as-is (unchanged signature/semantics) by the WAN punch-trial state
// machine below — the "did a fresh handshake land after we force-rekeyed"
// question is identical whether the pinned endpoint is a same-LAN ip:port or a
// STUN-discovered cross-network one.
func evaluateLANTrial(lastHandshake, preTrialHandshake, trialDeadline, now time.Time, freshWindow time.Duration) lanTrialOutcome {
	verified := !lastHandshake.IsZero() &&
		lastHandshake.After(preTrialHandshake) &&
		now.Sub(lastHandshake) < freshWindow
	if verified {
		return trialVerified
	}
	if !now.Before(trialDeadline) {
		return trialTimedOut
	}
	return trialPending
}

// ---------------------------------------------------------------------------
// Phase 3 — WAN NAT-punch direct-path trial/flip.
//
// Generalizes the Phase-2 LAN-trial machinery above to peers on DIFFERENT
// networks, coordinated via the coordinator's punch_coord broadcast rather
// than a same-LAN co-subnet match. Unlike LAN direct, this is NOT gated by
// POTLUCK_LAN_DIRECT — it runs whenever a DERP relay is configured (dBind !=
// nil), because DERP is the always-on floor this whole mechanism upgrades
// away from, independent of the opt-in same-LAN fast path.
//
// The three bugs this fixes (see cmd/potluck-mesh/main.go's OnPunchCoord and
// runPunchTrialLoop for the actual wiring):
//
//  1. The old OnPunchCoord guard vetoed EVERY punch permanently, because every
//     peer is proactively flipped to DERP on welcome/roster-add BEFORE the
//     punch fires, and the guard skipped the endpoint write once DERP-routed
//     (i.e. always). Fix: the guard now only skips when the peer is ALREADY in
//     a managed trial/direct mode (avoid stomping on an in-flight attempt) or
//     backed off — never a blanket "DERP-routed → skip".
//  2. A bare UpdatePeerEndpoint does not force wireguard-go to re-handshake on
//     a live (DERP-established) session — same root cause as the LAN trial's
//     force-rekey fix. This machinery therefore uses the identical
//     RemovePeer+AddPeer-pinned-with-short-keepalive pattern.
//  3. flipToDirectUDP (dead code — zero call sites before this change) only
//     cleared the DERP routing flag without pinning a verified endpoint, which
//     would have hit bug #2 again the moment it was wired up. It is removed;
//     the verified-promotion step here (see runPunchTrialLoop) does the
//     correct thing: re-add with the standard keepalive, pinned to the
//     endpoint that just produced a fresh handshake.
// ---------------------------------------------------------------------------

const (
	// punchTrialKeepalive mirrors lanTrialKeepalive's rationale: a short
	// persistent keepalive forces wireguard-go to initiate a handshake to the
	// newly-pinned (real, cross-network) endpoint promptly, even on an idle
	// mesh, rather than waiting up to ~2min (REKEY_AFTER_TIME) for the next
	// natural renegotiation.
	punchTrialKeepalive = 5

	// punchDirectKeepalive is re-applied once a punch trial is VERIFIED,
	// matching the standard mesh keepalive (see rosterPeerToTunnelPeer) so a
	// confirmed direct link stops the keepalive-spam of the trial period.
	punchDirectKeepalive = 25

	// punchTrialDuration bounds how long a punch trial is given to produce a
	// fresh handshake before rolling back to the DERP floor. Slightly longer
	// than lanTrialDuration (12s): a WAN round trip plus the simultaneous-send
	// hole-punch race (both sides' handshake-inits must cross in flight) needs
	// more headroom than a same-LAN trial, which is sub-ms to low-ms.
	punchTrialDuration = 15 * time.Second

	// punchTrialBackoff suppresses re-trialling a peer whose punch attempt
	// just failed (dead/blocked NAT combination, firewalled WG port, etc.),
	// for the same anti-thrash reason as lanTrialBackoff: without it, a peer
	// that keeps sending fresh endpoint_updates would trigger a punch_coord →
	// trial → timeout cycle every request_punch, repeatedly knocking the peer
	// off the working relay for punchTrialDuration each time.
	punchTrialBackoff = 5 * time.Minute

	// punchVerifyInterval is the cadence of the dedicated fast ticker that
	// checks in-progress punch trials for a fresh handshake (or an expired
	// deadline) and checks confirmed direct peers for staleness. Mirrors
	// lanVerifyInterval; kept as a separate constant since the two mechanisms
	// are independently gated (this one runs whenever dBind != nil).
	punchVerifyInterval = 2 * time.Second
)

// punchTrialState holds the in-progress (or most recently completed) WAN
// punch-trial bookkeeping for a single peer. Guarded by punchTrialTracker's
// mutex — written by OnPunchCoord (the signal-client goroutine) when a trial
// starts, and read/written by runPunchTrialLoop (its own goroutine) as the
// trial is verified or times out. This cross-goroutine sharing is exactly why
// it is a separate, explicitly-locked structure rather than reusing
// runReachabilityLoop's peerReachState, which is private to that loop's own
// goroutine.
type punchTrialState struct {
	endpoint          string    // the coordinator-supplied ip:port we force-rekeyed to
	preTrialHandshake time.Time // last-handshake snapshot taken at trial start
	deadline          time.Time // trial gives up after this
	backoffUntil      time.Time // suppress a new trial until this time
}

// punchTrialTracker is a tiny thread-safe map of base64-pubkey →
// *punchTrialState, shared between OnPunchCoord (trial start) and
// runPunchTrialLoop (verify / timeout / direct-health-check). Parallels
// modeTracker's concurrency story but carries the richer per-trial bookkeeping
// that modeTracker's single-enum-value map cannot.
type punchTrialTracker struct {
	mu     sync.Mutex
	states map[string]*punchTrialState
}

func newPunchTrialTracker() *punchTrialTracker {
	return &punchTrialTracker{states: make(map[string]*punchTrialState)}
}

// entry returns the peer's state, creating a zero-value entry on first use.
// Never returns nil.
func (pt *punchTrialTracker) entry(pubkeyBase64 string) *punchTrialState {
	pt.mu.Lock()
	defer pt.mu.Unlock()
	st, ok := pt.states[pubkeyBase64]
	if !ok {
		st = &punchTrialState{}
		pt.states[pubkeyBase64] = st
	}
	return st
}

// get returns the peer's state without creating one. ok is false if the peer
// has never started a trial.
func (pt *punchTrialTracker) get(pubkeyBase64 string) (punchTrialState, bool) {
	pt.mu.Lock()
	defer pt.mu.Unlock()
	st, ok := pt.states[pubkeyBase64]
	if !ok {
		return punchTrialState{}, false
	}
	return *st, ok // copy out — callers must not mutate the tracker's internals
}

// setBackoff records a new backoff deadline for the peer (called on trial
// timeout). Creates an entry if none exists.
func (pt *punchTrialTracker) setBackoff(pubkeyBase64 string, until time.Time) {
	pt.mu.Lock()
	defer pt.mu.Unlock()
	st, ok := pt.states[pubkeyBase64]
	if !ok {
		st = &punchTrialState{}
		pt.states[pubkeyBase64] = st
	}
	st.backoffUntil = until
}

// startTrial records a freshly-started trial's bookkeeping, overwriting any
// prior state for the peer (a new trial always supersedes stale data, but
// preserves nothing — backoffUntil from a PRIOR failed trial is intentionally
// cleared here because shouldStartPunchTrial already gated on it before this
// is called; carrying a stale backoff forward would be inert but confusing).
func (pt *punchTrialTracker) startTrial(pubkeyBase64, endpoint string, preTrialHandshake, deadline time.Time) {
	pt.mu.Lock()
	defer pt.mu.Unlock()
	pt.states[pubkeyBase64] = &punchTrialState{
		endpoint:          endpoint,
		preTrialHandshake: preTrialHandshake,
		deadline:          deadline,
	}
}

// remove drops a peer's punch-trial bookkeeping (called when a peer leaves
// the roster).
func (pt *punchTrialTracker) remove(pubkeyBase64 string) {
	pt.mu.Lock()
	defer pt.mu.Unlock()
	delete(pt.states, pubkeyBase64)
}

// prune drops bookkeeping for any peer not present in livePubkeys. Called
// periodically by runPunchTrialLoop as a safety net alongside the immediate
// remove() call in OnRosterUpdate's Removed handling.
func (pt *punchTrialTracker) prune(livePubkeys map[string]bool) {
	pt.mu.Lock()
	defer pt.mu.Unlock()
	for k := range pt.states {
		if !livePubkeys[k] {
			delete(pt.states, k)
		}
	}
}

// shouldStartPunchTrial reports whether a peer that is currently in modeDERP
// should begin a WAN punch trial. Conditions:
//   - the peer is in modeDERP (never re-trial out of an active trial/direct —
//     this also means a peer already lan-trial/lan-direct is implicitly
//     excluded, since the LAN mechanism owns those and never leaves them at
//     modeDERP while active),
//   - the peer is not in punch-trial backoff (a recently-failed trial),
//   - this node's own NAT is not classified as symmetric — see internal/stun's
//     NAT-type classifier. Client-side hole-punch cannot work when the local
//     external port varies per destination, so attempting a trial is a
//     guaranteed-failure retry loop; skip it entirely rather than burning
//     punchTrialDuration (and a DERP outage window) on every punch_coord.
//
// Pure function — no I/O — so it is unit-testable exactly like
// shouldStartLANTrial.
func shouldStartPunchTrial(mode routingMode, backoffUntil time.Time, symmetricNAT bool, now time.Time) bool {
	if mode != modeDERP {
		return false
	}
	if symmetricNAT {
		return false
	}
	if now.Before(backoffUntil) {
		return false
	}
	return true
}
