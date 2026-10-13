package main

import (
	"bytes"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/netip"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
	"github.com/potluck-ai/potluck-mesh/internal/util"
)

// Direct-path trials: who starts the handshake, and what counts as proof.
//
// Both trials (the WAN punch trial driven by punch_coord, and the LAN trial)
// clear the peer's WireGuard session and add the peer again pinned to the direct
// endpoint, so that wireguard-go has to handshake there. That has two pitfalls
// in the vendored wireguard-go (v0.0.0-20231211153847), visible when it is
// instrumented to log handshake indexes and keypair slots.
//
// 1. The coordinator sends punch_coord to both machines at the same moment, so
//    both removed and added the peer and both sent a handshake initiation within
//    a millisecond of each other. wireguard-go handles a pair of crossing
//    initiations badly in two ways:
//
//    - Each side consumes the other's initiation before the other's response
//      arrives. CreateMessageResponse deletes the side's own initiator index, so
//      the response to its own initiation is rejected ("Received invalid response
//      message"). Both sides end with only a responder keypair waiting for data
//      from an initiator that does not exist, no session at all. The persistent
//      keepalive timers (5 s in a trial, not jittered) then send the next
//      initiation on both sides at the same instant, so the crossing repeats every
//      5 s, over the relay as well, until the trial times out.
//
//    - The device runs several handshake workers. ConsumeMessageResponse checks
//      the handshake state under a read lock and writes the result under a second
//      lock. When the other worker consumes the peer's initiation, creates the
//      response and begins its session between the two, the first worker writes
//      into an already zeroed handshake and derives an initiator keypair whose
//      local index is 0. SwapIndexForKeypair ignores index 0, so that keypair is
//      never registered. The peer's packets address the deleted initiator index
//      and are dropped on arrival, one way, while handshakes and the peer's own
//      packets look healthy on both sides. A trial that verified on the
//      handshake time alone would keep the relay off in this state.
//
//    The answer is that one side starts the handshake. The side with the lower
//    public key does the standard trial sequence. The other side clears its session and adds the peer
//    with no persistent keepalive and no endpoint, so that nothing it sends can
//    become an initiation on the wire (with no session, wireguard-go starts a
//    handshake for any packet it is asked to send; with no endpoint the attempt has
//    nowhere to go). It learns the endpoint from the first side's initiation and
//    answers it. punchFollowerDelay later it either sets the trial keepalive on the
//    session that has come up, or, if none has (a NAT that needs an outbound packet
//    first, a lost initiation), adds the peer again pinned to the endpoint so that
//    its own initiation goes out, now no longer crossing the first.
//
// 2. A handshake time says that handshake messages crossed, not that packets flow
//    in both directions. A trial is verified only when the handshake is fresh
//    and at least one of two kinds of evidence has been seen since the peer was
//    last added (at the trial's start, or at its one retry):
//
//    - the probe was answered: a TCP connect from this machine's mesh address to
//      the peer's mesh address, through the tunnel, was accepted or refused. A SYN
//      has to reach the peer and the RST (or SYN-ACK) has to come back, so a
//      session that works in one direction only cannot pass. It needs the peer's
//      operating system to answer a port nothing listens on, and a machine in
//      stealth mode (the Windows Defender Firewall default; macOS when its stealth
//      mode is on) answers nothing for such a port;
//    - an inbound packet from the peer's mesh address came out of the tunnel (the
//      packet filter's inbound watch, internal/tunnel/filter_watch.go): the peer's
//      packets reach this machine and decrypt. It needs nothing from the peer's
//      operating system, only that the peer sends something. The peer's own probe
//      is that something: both machines of a new/new pair probe, so each sees the
//      other's SYNs whether or not either operating system ever answers.
//
//    The probe is kept for both reasons: it is the traffic that makes the peer's
//    inbound evidence, and where the peer's system answers it is the stronger proof.
//    Each machine sends its first probe at its first sight of a fresh handshake
//    even if inbound evidence is already there, because the peer may be waiting
//    for exactly that SYN.
//
//    One-way sessions: the machine whose inbound is dead sees neither evidence, so
//    after trialRekeyAfter it adds the peer again, alone (the other machine is not
//    starting a handshake), which gives both machines a clean session. The other
//    machine verifies on the first machine's SYNs, which is harmless. If the first
//    machine still sees no evidence after the retry it ends the trial with NO DATA
//    PATH, back on the relay with the backoff, and the other machine's packets
//    follow it there because wireguard-go roams a peer's endpoint to wherever its
//    authenticated packets arrive from. Where the other machine is also one whose
//    system answers nothing and it has already verified (so it no longer probes),
//    the first machine has no evidence even after the retry and ends on the relay.
//
//    A new sidecar paired with an older one gets no probe SYNs from it (the older
//    one does not probe). On a peer whose system answers resets the new sidecar
//    verifies by its probe. On a peer whose system does not, it sees no evidence
//    unless real traffic arrives from the peer, so it ends the trial with NO DATA
//    PATH and stays on the relay. That is the safe direction (the relay is the
//    floor) and it ends when both are upgraded.

const (
	// punchFollowerDelay is how long the side that does not start the handshake
	// waits before it sets its trial keepalive. Long enough for the other side's
	// initiation to arrive and be answered (a LAN round trip is well under a
	// millisecond, a WAN one tens), short enough for a NAT-restricted pair to
	// connect within about a second.
	punchFollowerDelay = 500 * time.Millisecond

	// trialProbePort is the TCP port the probe connects to on the peer's mesh
	// address: the discard port. Nothing listens there, so the peer's own network
	// stack answers with a reset, which is all the probe needs. If something does
	// listen, the connect succeeds and counts the same.
	trialProbePort = 9

	// trialProbeTimeout bounds one probe connect. A little over a second so that an
	// unanswered probe sends the SYN twice where the system retransmits after 1 s
	// (Linux, macOS), and still ends before the next 2 s verify tick.
	trialProbeTimeout = 1500 * time.Millisecond

	// trialProbeInterval is the least time between two probes of one peer.
	trialProbeInterval = time.Second

	// trialRekeyAfter is how long a fresh handshake may stand without a probe
	// answer before the peer is cleared and added again once. The verify tick is
	// 2 s and its timer jitters by a few milliseconds, so 3 s means "on the third
	// tick after the handshake was first seen": probes have been made on the first
	// two and have failed. A value of exactly two ticks would act on the third or
	// the fourth depending on the jitter.
	trialRekeyAfter = 3 * time.Second

	// trialNoPathAfter is the same wait, counted from the moment the peer was added
	// again, before the trial gives up and goes back to the relay. A dead path then
	// ends the trial 6 to 10 s after it started (the handshake is first seen up to
	// one tick late, then two more ticks, then two more), inside punchTrialDuration
	// (15 s).
	trialNoPathAfter = 3 * time.Second
)

// wsaeConnRefused is Windows' WSAECONNREFUSED. syscall.ECONNREFUSED on Windows
// is an invented value that no socket call returns, so the real number is
// compared as well. On other systems the constant never occurs.
const wsaeConnRefused syscall.Errno = 10061

// trialLeader reports whether this machine starts the handshake of a direct-path
// trial that both machines start at the same moment. The machine with the lower
// WireGuard public key (compared as raw bytes) does. The answer is the opposite
// on the two machines, whatever order the messages arrive in. When either key
// cannot be read the answer is true, which is what every trial did before.
func trialLeader(selfPubBase64, peerPubBase64 string) bool {
	self, err1 := base64.StdEncoding.DecodeString(selfPubBase64)
	peer, err2 := base64.StdEncoding.DecodeString(peerPubBase64)
	if err1 != nil || err2 != nil || len(self) != 32 || len(peer) != 32 {
		return true
	}
	return bytes.Compare(self, peer) < 0
}

// trialStep is what a verify tick should do about a trial peer.
type trialStep int

const (
	// stepPending: keep waiting.
	stepPending trialStep = iota
	// stepVerified: the handshake is fresh and there is evidence: the probe was
	// answered or a packet from the peer came out of the tunnel.
	stepVerified
	// stepTimedOut: the deadline passed with no usable handshake.
	stepTimedOut
	// stepRekey: a fresh handshake has stood without evidence; clear the peer and
	// add it again pinned to the endpoint (once per trial).
	stepRekey
	// stepNoPath: a fresh handshake stood without evidence to the end (or to the
	// deadline), after the one retry. Same outcome as a timeout.
	stepNoPath
)

func (s trialStep) String() string {
	switch s {
	case stepVerified:
		return "verified"
	case stepTimedOut:
		return "timed-out"
	case stepRekey:
		return "rekey"
	case stepNoPath:
		return "no-path"
	default:
		return "pending"
	}
}

// stepTrial combines the handshake verdict (evaluateLANTrial) with the evidence
// (evidence: the probe was answered or a packet from the peer came out of the
// tunnel). firstFresh is when a tick first saw a fresh handshake newer than the pre-trial
// snapshot, zero until then; rekeyed says the peer was already added again once.
// Pure, so it can be tested as a table.
func stepTrial(handshake lanTrialOutcome, evidence bool, firstFresh time.Time, rekeyed bool, deadline, now time.Time) trialStep {
	switch handshake {
	case trialTimedOut:
		return stepTimedOut
	case trialPending:
		return stepPending
	}
	// A fresh handshake. It is not enough on its own.
	if evidence {
		return stepVerified
	}
	if !now.Before(deadline) {
		return stepNoPath
	}
	if firstFresh.IsZero() {
		return stepPending
	}
	age := now.Sub(firstFresh)
	if !rekeyed && age >= trialRekeyAfter {
		return stepRekey
	}
	if age >= trialNoPathAfter {
		return stepNoPath
	}
	return stepPending
}

// probeTunnelPath makes one TCP connect from localIP to peerIP on trialProbePort,
// both mesh addresses, so the SYN is routed into the tunnel. It reports true when
// the connect was accepted or refused, that is when the peer's network stack
// answered, which needs the packet to have crossed the tunnel in both directions.
// Anything else (a timeout, no route, a filtered port) is false.
func probeTunnelPath(localIP, peerIP string, timeout time.Duration) bool {
	return probePort(localIP, peerIP, trialProbePort, timeout)
}

// probePort is probeTunnelPath with the port given, so tests can aim at a
// loopback listener.
func probePort(localIP, peerIP string, port int, timeout time.Duration) bool {
	lip := net.ParseIP(localIP)
	pip := net.ParseIP(peerIP)
	if lip == nil || pip == nil {
		return false
	}
	d := net.Dialer{Timeout: timeout, LocalAddr: &net.TCPAddr{IP: lip}}
	c, err := d.Dial("tcp", net.JoinHostPort(peerIP, strconv.Itoa(port)))
	if err == nil {
		_ = c.Close()
		return true
	}
	return connRefused(err)
}

func connRefused(err error) bool {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		return errno == syscall.ECONNREFUSED || errno == wsaeConnRefused
	}
	return false
}

// inboundWatcher counts the packets that come out of the tunnel from chosen
// addresses. *tunnel.PacketFilter is the implementation.
type inboundWatcher interface {
	WatchInbound(addr netip.Addr)
	UnwatchInbound(addr netip.Addr)
	InboundCount(addr netip.Addr) uint64
}

// Names of the kinds of evidence, for the log.
const (
	proofProbe   = "probe"
	proofInbound = "inbound"
)

// probeState is one peer's progress through the verification.
type probeState struct {
	gen        uint64 // bumped by begin, finish and markRekeyed; a probe that finishes under an older one is ignored
	ok         bool
	by         string // proofProbe or proofInbound, whichever came first
	inflight   bool
	probes     int // probes started since the peer was last added
	lastAt     time.Time
	firstFresh time.Time
	rekeyed    bool

	addr     netip.Addr // the peer's mesh address, watched while the trial runs
	watching bool
	baseline uint64 // inbound count when the peer was last added
}

// trialVerifier holds the verification state of the peers in a direct-path trial.
// It is shared by the WAN punch-trial loop and the LAN-trial loop, which run on
// different goroutines. A nil *trialVerifier means "no verification": callers then
// verify on the handshake alone, as they did before.
type trialVerifier struct {
	mu     sync.Mutex
	peers  map[string]*probeState
	meshIP func() string                                  // this machine's mesh address
	watch  inboundWatcher                                 // nil: probe answers are the only evidence
	probe  func(local, peer string, d time.Duration) bool // probeTunnelPath, replaceable in tests
	done   func()                                         // test hook: called when a probe finishes
}

func newTrialVerifier(meshIP func() string, watch inboundWatcher) *trialVerifier {
	return &trialVerifier{peers: make(map[string]*probeState), meshIP: meshIP, watch: watch, probe: probeTunnelPath}
}

func (v *trialVerifier) state(pubkey string) *probeState {
	st, ok := v.peers[pubkey]
	if !ok {
		st = &probeState{}
		v.peers[pubkey] = st
	}
	return st
}

// unwatchLocked stops watching st's address. v.mu is held.
func (v *trialVerifier) unwatchLocked(st *probeState) {
	if st.watching && v.watch != nil {
		v.watch.UnwatchInbound(st.addr)
	}
	st.watching = false
}

// begin starts a peer's verification, called right after the peer was added at the
// start of a trial. It forgets earlier progress and starts watching the peer's
// inbound packets from now: the peer was just removed and added, so nothing the
// old session carried can still arrive, and any packet counted from here on
// decrypted under the new session.
func (v *trialVerifier) begin(pubkey, peerMeshIP string) {
	if v == nil {
		return
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	st := v.state(pubkey)
	v.unwatchLocked(st)
	gen := st.gen + 1
	*st = probeState{gen: gen}
	v.watchLocked(st, peerMeshIP)
}

func (v *trialVerifier) watchLocked(st *probeState, peerMeshIP string) {
	if v.watch == nil {
		return
	}
	if a, err := netip.ParseAddr(peerMeshIP); err == nil {
		st.addr = a.Unmap()
		v.watch.WatchInbound(st.addr)
		st.watching = true
		st.baseline = v.watch.InboundCount(st.addr)
	}
}

// finish ends a peer's verification (verified, timed out, given up on, or the trial
// was cancelled) and stops watching it.
func (v *trialVerifier) finish(pubkey string) {
	if v == nil {
		return
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	st, ok := v.peers[pubkey]
	if !ok {
		return
	}
	v.unwatchLocked(st)
	gen := st.gen + 1
	*st = probeState{gen: gen}
}

// proof says which evidence verified the peer ("probe" or "inbound"), or "".
func (v *trialVerifier) proof(pubkey string) string {
	if v == nil {
		return ""
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	if st := v.peers[pubkey]; st != nil && st.ok {
		return st.by
	}
	return ""
}

// markRekeyed records that the peer was added again at now: the clock starts again
// from now, the one retry is spent, and only evidence that arrives after the retry
// counts. Call it after the peer was added.
func (v *trialVerifier) markRekeyed(pubkey string, now time.Time) {
	if v == nil {
		return
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	st := v.state(pubkey)
	gen := st.gen + 1
	next := probeState{gen: gen, rekeyed: true, firstFresh: now, addr: st.addr, watching: st.watching}
	if next.watching && v.watch != nil {
		next.baseline = v.watch.InboundCount(next.addr)
	}
	*st = next
}

// observe is called on a verify tick for a peer whose handshake is fresh. It
// records when the handshake was first seen fresh, notes inbound evidence, starts
// a probe in the background when none is running and the last one is old enough
// (always the first one, whether or not the peer has been heard from: it may be
// waiting for this machine's SYN), and returns the current state. ok means
// evidence of either kind has been seen.
func (v *trialVerifier) observe(pubkey, peerMeshIP string, now time.Time) (ok bool, firstFresh time.Time, rekeyed bool) {
	v.mu.Lock()
	defer v.mu.Unlock()
	st := v.state(pubkey)
	if st.firstFresh.IsZero() {
		st.firstFresh = now
	}
	if !st.ok && st.watching && v.watch.InboundCount(st.addr) > st.baseline {
		st.ok, st.by = true, proofInbound
	}
	if !st.inflight && now.Sub(st.lastAt) >= trialProbeInterval && (!st.ok || st.probes == 0) {
		if local := v.meshIP(); local != "" && peerMeshIP != "" {
			st.inflight = true
			st.probes++
			st.lastAt = now
			gen := st.gen
			go v.run(pubkey, gen, local, peerMeshIP)
		}
	}
	return st.ok, st.firstFresh, st.rekeyed
}

func (v *trialVerifier) run(pubkey string, gen uint64, local, peer string) {
	start := time.Now()
	ok := v.probe(local, peer, trialProbeTimeout)
	slog.Debug("trial probe through the tunnel",
		"pubkey_prefix", util.TruncatePubkey(pubkey),
		"answered", ok,
		"took_ms", time.Since(start).Milliseconds(),
	)
	v.mu.Lock()
	if st := v.peers[pubkey]; st != nil && st.gen == gen {
		st.inflight = false
		if ok && !st.ok {
			st.ok, st.by = true, proofProbe
		}
	}
	done := v.done
	v.mu.Unlock()
	if done != nil {
		done()
	}
}

// prune drops the state of peers that are no longer in the roster.
func (v *trialVerifier) prune(live map[string]bool) {
	if v == nil {
		return
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	for k, st := range v.peers {
		if !live[k] {
			v.unwatchLocked(st)
			delete(v.peers, k)
		}
	}
}

// canProbe reports whether a probe can run for this peer at all: this machine's
// mesh address and the peer's are both known. When it cannot, callers verify on
// the handshake alone.
func (v *trialVerifier) canProbe(peerMeshIP string) bool {
	return v != nil && v.meshIP != nil && v.meshIP() != "" && peerMeshIP != ""
}

// trialStepFor is stepTrial with the verifier's probe state. With no verifier, or
// when a probe cannot run (this machine's or the peer's mesh address unknown), the
// handshake alone decides, as it did before the probe existed.
func trialStepFor(v *trialVerifier, pubkey, peerMeshIP string, handshake lanTrialOutcome, deadline, now time.Time) trialStep {
	if handshake != trialVerified || !v.canProbe(peerMeshIP) {
		switch handshake {
		case trialVerified:
			return stepVerified
		case trialTimedOut:
			return stepTimedOut
		default:
			return stepPending
		}
	}
	ok, firstFresh, rekeyed := v.observe(pubkey, peerMeshIP, now)
	return stepTrial(handshake, ok, firstFresh, rekeyed, deadline, now)
}

// repinTrialPeer clears the peer's WireGuard session and adds the peer again
// pinned to endpoint with the given persistent keepalive, which makes
// wireguard-go send a new initiation at once. The same two steps a trial starts
// with.
func repinTrialPeer(tun rosterTunnel, pubkey, meshIP, endpoint string, keepalive int) error {
	if err := tun.RemovePeer(pubkey); err != nil {
		return fmt.Errorf("remove peer: %w", err)
	}
	return tun.AddPeer(tunnel.Peer{
		PublicKey:           pubkey,
		AllowedIPs:          []string{meshIP + "/32"},
		Endpoint:            endpoint,
		PersistentKeepalive: keepalive,
	})
}
