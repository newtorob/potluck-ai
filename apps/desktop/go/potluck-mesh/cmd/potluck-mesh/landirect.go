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
//   - modeDERP:      peer is relayed via the DERP relay (the default / floor).
//   - modeLANTrial:  peer was just pointed at a same-LAN endpoint; we are
//     waiting to VERIFY a fresh handshake before declaring success. DERP is OFF
//     during the trial, so a dead LAN path means the peer is unreachable until
//     the trial deadline rolls it back to DERP.
//   - modeLANDirect: a fresh handshake was observed on the LAN endpoint; the
//     peer is confirmed reachable directly and stays off the relay.
//
// All three are "managed" modes: signal-driven raw-endpoint updates
// (roster_update.Updated, punch_coord) MUST NOT overwrite the WG endpoint while
// a peer is in any of them, or they would clobber either the synthetic DERP
// endpoint (modeDERP) or the LAN endpoint we are trialling/using.
type routingMode int

const (
	modeDERP routingMode = iota
	modeLANTrial
	modeLANDirect
)

func (m routingMode) String() string {
	switch m {
	case modeLANTrial:
		return "lan-trial"
	case modeLANDirect:
		return "lan-direct"
	default:
		return "derp"
	}
}

// lanTrialDuration is how long a LAN-trial peer is given to produce a fresh
// handshake before we give up and roll back to DERP.
//
// Trade-off (justified): during the trial DERP is OFF, so a DEAD LAN path means
// the peer is unreachable for the whole trial window. WireGuard fires a
// handshake-init the moment the endpoint changes and retries roughly every 5s
// (REKEY_TIMEOUT); persistent keepalive is 25s. On a live LAN the first
// handshake-init round-trips in ~1 RTT (sub-millisecond to low-ms), so a healthy
// path is verified almost immediately. We pick 8s: long enough to absorb one
// retry plus jitter and clock skew between the two nodes, short enough that a
// dead LAN candidate only costs ~8s of (recoverable) unreachability before the
// peer is restored to the working relay. We deliberately do NOT wait a full
// keepalive interval — that would triple the dead-path outage for no gain on a
// healthy path.
const lanTrialDuration = 8 * time.Second

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

// isManaged reports whether the peer is in a LAN-managed mode (lan-trial or
// lan-direct) — the case where dBind.IsPeerDERPRouted would (correctly) report
// false yet a raw-endpoint overwrite must still be blocked. modeDERP / unknown
// returns false so the caller falls through to the dBind DERP check; that is
// what makes the guard identical to legacy behaviour when LAN direct is off.
func (t *modeTracker) isManaged(pubkeyBase64 string) bool {
	m := t.get(pubkeyBase64)
	return m == modeLANTrial || m == modeLANDirect
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
