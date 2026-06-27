// Package derp — DerpBind wires the DERP relay client into the wg-go
// conn.Bind interface so that:
//
//  1. Packets received from the DERP relay are injected into the WireGuard
//     device as if they arrived via UDP from a synthetic endpoint. WG can then
//     decrypt, validate, and deliver them to the TUN interface as normal.
//
//  2. When wg-go wants to send a packet and the destination endpoint is a
//     synthetic one (encoding a peer's WG pubkey), the packet is forwarded via
//     the DERP relay instead of being sent over raw UDP.
//
// # Synthetic endpoint encoding
//
// We use approach (A) from the design doc: a custom syntheticEndpoint type that
// implements conn.Endpoint. The type encodes the destination WG public key
// directly; no IP address space is consumed and the routing decision is made
// purely in Go.
//
// DstToString() returns "derp:<8-char-hex-prefix>" for logging. DstIP() /
// SrcIP() return the zero netip.Addr — they are only used by the OS network
// stack path which is bypassed for synthetic endpoints.
//
// # Concurrency
//
// - injectCh is a buffered channel (injectChanCap). The DERP read-loop goroutine
//   calls Inject(); the wg-go receive-func goroutine drains it.
// - derpPeers is guarded by derpMu. SetPeerRouting is called from the main
//   goroutine; Send is called from one of wg-go's send-loop goroutines.
// - closeState is held in atomic.Pointer so Open and Close can race safely.
//   Each Open installs a fresh *closeState (its own channel + sync.Once), and
//   Close atomically loads-and-clears the current state, running its Once at
//   most once. No mutex is involved on the close path, eliminating the
//   prior write-write race on the sync.Once value.
// - The inner StdNetBind handles all its own concurrency.
//
// # Lifecycle
//
// DerpBind.Open is called by wg-go once when the device starts. It calls the
// inner bind's Open and appends our own ReceiveFunc. Close propagates to the
// inner bind and closes the injectCh so the ReceiveFunc goroutine exits cleanly.
//
// # Privacy
//
// - Never log payload bytes.
// - Truncate pubkeys to 8 hex chars in all log output.
package derp

import (
	"fmt"
	"log/slog"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"

	"golang.zx2c4.com/wireguard/conn"

	"github.com/potluck-ai/potluck-mesh/internal/util"
)

const (
	// injectChanCap is the buffer size for the DERP-inject channel.
	// Deep enough to absorb a burst of WG handshake retries without blocking
	// the DERP read loop.
	injectChanCap = 256
)

// ---------------------------------------------------------------------------
// syntheticEndpoint — implements conn.Endpoint for DERP-routed peers
// ---------------------------------------------------------------------------

// syntheticEndpoint encodes a WireGuard peer pubkey as a conn.Endpoint.
// wg-go uses the endpoint to match incoming packets to a peer session
// (by comparing it against the endpoint stored in the peer table). As long as
// we always use the same syntheticEndpoint for the same pubkey, WG's session
// lookup works correctly without any actual UDP address.
type syntheticEndpoint struct {
	pubkey [32]byte
}

// DstToString returns a human-readable string for logging / UAPI display.
// Format: "derp:<8-char-hex-prefix>" — never includes full key material.
func (e syntheticEndpoint) DstToString() string {
	return fmt.Sprintf("derp:%x", e.pubkey[:4])
}

// SrcToString returns an empty string — source address has no meaning for
// a relay-routed packet.
func (e syntheticEndpoint) SrcToString() string { return "" }

// DstIP returns a zero AddrPort — synthetic endpoints have no IP address.
func (e syntheticEndpoint) DstIP() netip.Addr { return netip.Addr{} }

// SrcIP returns a zero Addr — no source IP for relay-routed traffic.
func (e syntheticEndpoint) SrcIP() netip.Addr { return netip.Addr{} }

// DstToBytes returns the raw destination address bytes. wg-go uses this for
// fast endpoint-equality checks. We return the WG pubkey itself — the stable
// identity for a synthetic endpoint, uniquely identifying the routing target.
func (e syntheticEndpoint) DstToBytes() []byte {
	out := make([]byte, len(e.pubkey))
	copy(out, e.pubkey[:])
	return out
}

// ClearSrc is a no-op; synthetic endpoints have no source state to clear.
func (e syntheticEndpoint) ClearSrc() {}

// syntheticEndpointKey is the prefix used in ParseEndpoint to identify our
// synthetic format. We use a "derp:" URL-like prefix that cannot collide with
// real "ip:port" endpoint strings.
const syntheticEndpointKey = "derp:"

// SyntheticEndpointString returns the string representation used as the WG
// peer endpoint when configuring DERP-routed peers via UAPI / UpdatePeerEndpoint.
// This is what gets passed back to ParseEndpoint, so it must round-trip cleanly.
func SyntheticEndpointString(pubkey [32]byte) string {
	return fmt.Sprintf("derp:%x", pubkey[:])
}

// parseSyntheticEndpoint parses a string written by SyntheticEndpointString.
// Returns (ep, true) if the string matches; (zero, false) otherwise.
func parseSyntheticEndpoint(s string) (syntheticEndpoint, bool) {
	if len(s) != len(syntheticEndpointKey)+64 {
		return syntheticEndpoint{}, false
	}
	if s[:len(syntheticEndpointKey)] != syntheticEndpointKey {
		return syntheticEndpoint{}, false
	}
	var key [32]byte
	hexPart := s[len(syntheticEndpointKey):]
	for i := 0; i < 32; i++ {
		b, err := parseHexByte(hexPart[i*2], hexPart[i*2+1])
		if err != nil {
			return syntheticEndpoint{}, false
		}
		key[i] = b
	}
	return syntheticEndpoint{pubkey: key}, true
}

func parseHexByte(hi, lo byte) (byte, error) {
	h, err := hexNibble(hi)
	if err != nil {
		return 0, err
	}
	l, err := hexNibble(lo)
	if err != nil {
		return 0, err
	}
	return (h << 4) | l, nil
}

func hexNibble(c byte) (byte, error) {
	switch {
	case c >= '0' && c <= '9':
		return c - '0', nil
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10, nil
	case c >= 'A' && c <= 'F':
		return c - 'A' + 10, nil
	}
	return 0, fmt.Errorf("invalid hex nibble: %c", c)
}

// ---------------------------------------------------------------------------
// injectedPacket — unit of work queued from Inject → ReceiveFunc
// ---------------------------------------------------------------------------

type injectedPacket struct {
	src     [32]byte
	payload []byte
}

// ---------------------------------------------------------------------------
// DerpBind — conn.Bind implementation
// ---------------------------------------------------------------------------

// closeState bundles a closeCh and the sync.Once that closes it for a single
// Open/Close cycle. A fresh *closeState is installed by Open and atomically
// swapped out by Close, so the Once value is never re-used or shared across
// concurrent Close paths.
type closeState struct {
	ch   chan struct{}
	once sync.Once
}

// DerpBind wraps wg-go's conn.StdNetBind and intercepts:
//   - Outbound sends to syntheticEndpoints: routed via DERP relay.
//   - Inbound DERP packets: injected into the ReceiveFunc channel.
//
// For all other traffic (real UDP endpoints) it delegates to the inner bind.
type DerpBind struct {
	inner  conn.Bind
	client *Client

	injectCh chan injectedPacket // Inject → wg-go receive-func

	// cs holds the *closeState for the current Open cycle. Nil means
	// "no active Open" — Close on a nil pointer is a safe no-op.
	cs atomic.Pointer[closeState]

	derpMu    sync.RWMutex
	derpPeers map[[32]byte]bool // pubkey → true if DERP-routed
}

// NewDerpBind creates a DerpBind wrapping the provided inner bind and DERP client.
// Call tunnel.New(..., tunnel.WithBind(b)) to install it in the WG device.
func NewDerpBind(inner conn.Bind, client *Client) *DerpBind {
	b := &DerpBind{
		inner:     inner,
		client:    client,
		injectCh:  make(chan injectedPacket, injectChanCap),
		derpPeers: make(map[[32]byte]bool),
	}
	// Install an initial closeState so callers that hit Close before Open
	// (defensive, unusual) still find a usable Once.
	b.cs.Store(&closeState{ch: make(chan struct{})})
	return b
}

// Inject enqueues a packet received from the DERP relay for delivery into the
// WireGuard device. Called by the DERP read-loop goroutine; must not block.
// Drops the packet with a warning if the inject channel is full.
func (b *DerpBind) Inject(src [32]byte, payload []byte) {
	// Copy payload — the DERP read loop reuses its buffer across calls.
	pkt := injectedPacket{src: src, payload: make([]byte, len(payload))}
	copy(pkt.payload, payload)

	select {
	case b.injectCh <- pkt:
	default:
		slog.Warn("derp_bind: inject channel full, dropping inbound DERP packet",
			"src_prefix", util.TruncatePubkeyBytes(src),
			"payload_len", len(payload),
		)
	}
}

// SetPeerRouting marks (or unmarks) a peer as DERP-routed. When viaDERP is
// true, Send() for that peer's syntheticEndpoint is dispatched to the relay.
// Thread-safe; may be called from any goroutine.
func (b *DerpBind) SetPeerRouting(pubkey [32]byte, viaDERP bool) {
	b.derpMu.Lock()
	defer b.derpMu.Unlock()
	if viaDERP {
		b.derpPeers[pubkey] = true
		slog.Info("derp_bind: peer routed via DERP",
			"pubkey_prefix", util.TruncatePubkeyBytes(pubkey),
		)
	} else {
		delete(b.derpPeers, pubkey)
		slog.Info("derp_bind: peer reverted to direct UDP",
			"pubkey_prefix", util.TruncatePubkeyBytes(pubkey),
		)
	}
}

// IsPeerDERPRouted reports whether the given peer pubkey is currently
// configured to route via the DERP relay. Thread-safe.
func (b *DerpBind) IsPeerDERPRouted(pubkey [32]byte) bool {
	b.derpMu.RLock()
	defer b.derpMu.RUnlock()
	return b.derpPeers[pubkey]
}

// ---------------------------------------------------------------------------
// conn.Bind interface implementation
// ---------------------------------------------------------------------------

// Open calls the inner bind's Open and appends our DERP-inject ReceiveFunc.
// wg-go spawns one goroutine per ReceiveFunc and calls it in a loop.
//
// Note on lifecycle: wg-go's BindUpdate calls bind.Close() before calling
// bind.Open() — even on first startup, because closeBindLocked runs
// unconditionally and is a no-op only when netc.bind is nil (it isn't, since
// the bind was attached at device construction). That means by the time we
// get here, the previous closeState may have had its Once consumed. Install
// a fresh *closeState atomically so derpRecv doesn't immediately bail via
// the closeCh case on its first invocation.
func (b *DerpBind) Open(port uint16) ([]conn.ReceiveFunc, uint16, error) {
	innerFns, actualPort, err := b.inner.Open(port)
	if err != nil {
		return nil, 0, err
	}

	// Drain any packets that arrived during the Close window (or were left in
	// the buffer from the previous Open cycle). Without this drain, a stale
	// WG handshake packet from a prior session could be replayed into the new
	// session, confusing the WG state machine. Non-blocking: any packet that
	// arrives after this loop is a legitimately new delivery for this cycle.
	for {
		select {
		case <-b.injectCh:
		default:
			goto drained
		}
	}
drained:

	// Install a fresh closeState atomically. Any concurrent Close that
	// happens to race with this Store will either:
	//   (a) Load the previous closeState (already-fired Once → no-op), or
	//   (b) Load this new closeState (fires once, closes the new ch).
	// Either way, the Once value is never written-while-read.
	cs := &closeState{ch: make(chan struct{})}
	b.cs.Store(cs)
	closeCh := cs.ch // capture for closure stability

	// One-shot logger to confirm wg-go calls our ReceiveFunc at all. INFO-level
	// to make the wiring visible without enabling DEBUG everywhere.
	var firstCall sync.Once
	derpRecv := func(packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
		firstCall.Do(func() {
			slog.Info("derp_bind: wg-go entered derpRecv for the first time",
				"packets_cap", len(packets),
				"sizes_cap", len(sizes),
				"eps_cap", len(eps),
			)
		})
		select {
		case <-closeCh:
			slog.Info("derp_bind: derpRecv exiting via closeCh")
			return 0, net.ErrClosed
		case pkt, ok := <-b.injectCh:
			if !ok {
				slog.Info("derp_bind: derpRecv exiting via injectCh closed")
				return 0, net.ErrClosed
			}
			// Grow the packet buffer if needed. wg-go pre-allocates packets[0]
			// with an MTU-sized slice; extend rather than replace.
			if cap(packets[0]) >= len(pkt.payload) {
				packets[0] = packets[0][:len(pkt.payload)]
			} else {
				packets[0] = make([]byte, len(pkt.payload))
			}
			copy(packets[0], pkt.payload)
			sizes[0] = len(pkt.payload)
			eps[0] = syntheticEndpoint{pubkey: pkt.src}
			slog.Info("derp_bind: delivered DERP packet to WG device",
				"src_prefix", util.TruncatePubkeyBytes(pkt.src),
				"payload_len", len(pkt.payload),
			)
			return 1, nil
		}
	}

	return append(innerFns, derpRecv), actualPort, nil
}

// Close shuts down the DERP inject path and propagates to the inner bind.
// Safe to call concurrently with Open: each closeState's Once is private to
// its Open cycle, so multiple Closes simply fire each cycle's Once at most
// once and never write to the same Once value.
func (b *DerpBind) Close() error {
	if cs := b.cs.Load(); cs != nil {
		cs.once.Do(func() {
			close(cs.ch)
		})
	}
	return b.inner.Close()
}

// SetMark delegates to the inner bind.
func (b *DerpBind) SetMark(mark uint32) error {
	return b.inner.SetMark(mark)
}

// Send routes each buffer to either DERP (synthetic endpoint) or UDP (inner bind).
//
// wg-go batches sends: one ep but potentially multiple bufs. For DERP we send
// each buffer individually — the relay framing handles one WG packet per frame.
// Errors from individual DERP sends are logged but not propagated; DERP is
// best-effort and WG's own retry/rekeying logic will handle retransmit.
func (b *DerpBind) Send(bufs [][]byte, ep conn.Endpoint) error {
	se, ok := ep.(syntheticEndpoint)
	if !ok {
		// Not a synthetic endpoint — delegate to the inner UDP bind.
		// Log at INFO so we can SEE when WG is falling through to direct UDP
		// despite our DERP flip. If this fires for a peer we expect to be
		// DERP-routed, the endpoint got reverted somewhere upstream.
		slog.Info("derp_bind: Send via inner UDP (non-synthetic endpoint)",
			"endpoint_type", fmt.Sprintf("%T", ep),
			"bufs", len(bufs),
		)
		return b.inner.Send(bufs, ep)
	}

	// Confirm this peer is still in DERP-routing mode. If SetPeerRouting was
	// called to revert to UDP after wg-go resolved the endpoint, we should
	// not silently black-hole the packet — log and drop.
	b.derpMu.RLock()
	routed := b.derpPeers[se.pubkey]
	b.derpMu.RUnlock()

	if !routed {
		slog.Info("derp_bind: Send to synthetic endpoint but peer not in DERP map, dropping",
			"dst_prefix", util.TruncatePubkeyBytes(se.pubkey),
			"bufs", len(bufs),
		)
		return nil
	}

	for _, buf := range bufs {
		if err := b.client.Send(se.pubkey, buf); err != nil {
			slog.Info("derp_bind: relay send failed",
				"dst_prefix", util.TruncatePubkeyBytes(se.pubkey),
				"payload_len", len(buf),
				"err", err,
			)
		}
	}
	return nil
}

// ParseEndpoint checks whether the string encodes a synthetic DERP endpoint
// (written by SyntheticEndpointString), otherwise delegates to the inner bind.
func (b *DerpBind) ParseEndpoint(s string) (conn.Endpoint, error) {
	if se, ok := parseSyntheticEndpoint(s); ok {
		return se, nil
	}
	return b.inner.ParseEndpoint(s)
}

// BatchSize returns the inner bind's batch size. We do not change batching
// behaviour for real UDP packets; DERP inject is always 1 packet at a time.
func (b *DerpBind) BatchSize() int {
	return b.inner.BatchSize()
}
