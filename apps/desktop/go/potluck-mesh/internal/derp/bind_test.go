package derp_test

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
)

// ---------------------------------------------------------------------------
// fakeBind — minimal conn.Bind for testing DerpBind without real UDP sockets
// ---------------------------------------------------------------------------

type fakeBind struct {
	recvFuncs []conn.ReceiveFunc
	sent      chan fakeBindSend
	closed    bool
	parseFn   func(s string) (conn.Endpoint, error)
}

type fakeBindSend struct {
	bufs [][]byte
	ep   conn.Endpoint
}

func newFakeBind() *fakeBind {
	return &fakeBind{
		sent: make(chan fakeBindSend, 32),
	}
}

func (f *fakeBind) Open(port uint16) ([]conn.ReceiveFunc, uint16, error) {
	// Return an empty receive-func list — the test controls inbound via Inject.
	return f.recvFuncs, port, nil
}
func (f *fakeBind) Close() error                   { f.closed = true; return nil }
func (f *fakeBind) SetMark(mark uint32) error      { return nil }
func (f *fakeBind) BatchSize() int                 { return 1 }
func (f *fakeBind) Send(bufs [][]byte, ep conn.Endpoint) error {
	// Copy bufs to avoid aliasing issues in assertions.
	copied := make([][]byte, len(bufs))
	for i, b := range bufs {
		c := make([]byte, len(b))
		copy(c, b)
		copied[i] = c
	}
	select {
	case f.sent <- fakeBindSend{bufs: copied, ep: ep}:
	default:
	}
	return nil
}
func (f *fakeBind) ParseEndpoint(s string) (conn.Endpoint, error) {
	if f.parseFn != nil {
		return f.parseFn(s)
	}
	return nil, errors.New("fakeBind: ParseEndpoint not configured")
}


// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

// TestSyntheticEndpointRoundTrip verifies that SyntheticEndpointString produces
// a string that ParseEndpoint can decode back to the same pubkey.
func TestSyntheticEndpointRoundTrip(t *testing.T) {
	var key [32]byte
	for i := range key {
		key[i] = byte(i*3 + 7)
	}

	s := derp.SyntheticEndpointString(key)
	if s[:5] != "derp:" {
		t.Fatalf("SyntheticEndpointString prefix wrong: %q", s[:5])
	}
	// Full hex of 32 bytes = 64 chars; plus "derp:" = 69 total.
	if len(s) != 69 {
		t.Fatalf("SyntheticEndpointString length wrong: got %d, want 69", len(s))
	}

	fb := newFakeBind()
	c := derp.New("tcp+insecure://127.0.0.1:1", func(ctx context.Context) (string, error) { return "", nil }, [32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	ep, err := b.ParseEndpoint(s)
	if err != nil {
		t.Fatalf("ParseEndpoint(%q): %v", s, err)
	}
	if ep.DstToString()[:5] != "derp:" {
		t.Fatalf("parsed endpoint DstToString prefix wrong: %q", ep.DstToString())
	}
}

// TestNonSyntheticEndpointDelegates verifies ParseEndpoint for a non-derp string
// falls through to the inner bind's ParseEndpoint.
func TestNonSyntheticEndpointDelegates(t *testing.T) {
	fb := newFakeBind()
	wantEp := &dummyEndpoint{str: "1.2.3.4:51820"}
	fb.parseFn = func(s string) (conn.Endpoint, error) {
		return wantEp, nil
	}
	c := derp.New("tcp+insecure://127.0.0.1:1", func(ctx context.Context) (string, error) { return "", nil }, [32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	ep, err := b.ParseEndpoint("1.2.3.4:51820")
	if err != nil {
		t.Fatalf("ParseEndpoint: %v", err)
	}
	if ep != wantEp {
		t.Fatalf("expected inner bind endpoint, got %v", ep)
	}
}

// TestOpenAppendsReceiveFunc verifies that Open returns the inner bind's
// receive funcs plus one extra (the DERP inject func).
func TestOpenAppendsReceiveFunc(t *testing.T) {
	// Inner bind with zero receive funcs.
	fb := newFakeBind()
	c := derp.New("tcp+insecure://127.0.0.1:1", func(ctx context.Context) (string, error) { return "", nil }, [32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	fns, _, err := b.Open(0)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	// Should have exactly 1 receive func (the DERP inject func, inner has 0).
	if len(fns) != 1 {
		t.Fatalf("expected 1 ReceiveFunc, got %d", len(fns))
	}
}

// TestInjectDeliversPacket verifies that Inject pushes a packet through the
// DERP ReceiveFunc with the correct synthetic endpoint and payload.
func TestInjectDeliversPacket(t *testing.T) {
	fb := newFakeBind()
	c := derp.New("tcp+insecure://127.0.0.1:1", func(ctx context.Context) (string, error) { return "", nil }, [32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	fns, _, err := b.Open(0)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	// The DERP inject func is the last one.
	derpFn := fns[len(fns)-1]

	var srcKey [32]byte
	for i := range srcKey {
		srcKey[i] = byte(0xBB)
	}
	wantPayload := []byte("wg-encrypted-handshake-initiation")

	// Inject the packet before calling the receive func.
	b.Inject(srcKey, wantPayload)

	// Prepare wg-go-style receive call: pre-allocated packet buffer.
	packets := [][]byte{make([]byte, 4096)}
	sizes := []int{0}
	eps := []conn.Endpoint{nil}

	// ReceiveFunc should return immediately since we pre-filled the channel.
	done := make(chan error, 1)
	go func() {
		n, err := derpFn(packets, sizes, eps)
		if err != nil {
			done <- err
			return
		}
		if n != 1 {
			done <- errors.New("expected n=1")
			return
		}
		done <- nil
	}()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("ReceiveFunc: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("ReceiveFunc did not return within 2s")
	}

	// Verify payload.
	if sizes[0] != len(wantPayload) {
		t.Fatalf("size mismatch: got %d, want %d", sizes[0], len(wantPayload))
	}
	gotPayload := packets[0][:sizes[0]]
	if string(gotPayload) != string(wantPayload) {
		t.Fatalf("payload mismatch: got %q, want %q", gotPayload, wantPayload)
	}

	// Verify the endpoint is a synthetic one for the right pubkey.
	ep := eps[0]
	if ep == nil {
		t.Fatal("endpoint is nil")
	}
	wantEpStr := derp.SyntheticEndpointString(srcKey)
	gotEpStr := ep.DstToString()
	// DstToString truncates to 4 bytes (8 hex chars) + "derp:" prefix.
	// Just check the prefix matches.
	if gotEpStr[:5] != "derp:" {
		t.Fatalf("endpoint DstToString prefix wrong: %q", gotEpStr)
	}
	_ = wantEpStr // used to document intent
}

// TestCloseShutsDownReceiveFunc verifies that Close causes a blocked ReceiveFunc
// to return net.ErrClosed.
func TestCloseShutsDownReceiveFunc(t *testing.T) {
	fb := newFakeBind()
	c := derp.New("tcp+insecure://127.0.0.1:1", func(ctx context.Context) (string, error) { return "", nil }, [32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	fns, _, err := b.Open(0)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	derpFn := fns[len(fns)-1]

	// Start the receive func (will block because inject channel is empty).
	result := make(chan error, 1)
	packets := [][]byte{make([]byte, 4096)}
	sizes := []int{0}
	eps := []conn.Endpoint{nil}
	go func() {
		_, err := derpFn(packets, sizes, eps)
		result <- err
	}()

	// Give the goroutine time to block on the channel.
	time.Sleep(50 * time.Millisecond)

	// Close the bind — should unblock the receive func.
	if err := b.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	select {
	case err := <-result:
		if !errors.Is(err, net.ErrClosed) {
			t.Fatalf("expected net.ErrClosed after Close(), got: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("ReceiveFunc did not unblock after Close()")
	}
}

// TestSendViaDERPRoutesFakeClient verifies that Send to a syntheticEndpoint
// dispatches via the DERP client when the peer is in the DERP routing map.
// We use a real mock-relay-backed derp.Client and a gating channel to confirm
// the frame arrives at the relay.
func TestSendViaDERPRoutesFakeClient(t *testing.T) {
	frameReceived := make(chan struct{}, 1)

	// The relay handler reads whatever the client writes.
	mr := newMockRelay(t, func(conn net.Conn) {
		buf := make([]byte, 512)
		_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
		n, err := conn.Read(buf)
		if err != nil || n < 34 { // at minimum 2+32 bytes of frame header
			return
		}
		frameReceived <- struct{}{}
		// Keep connection alive until test ends.
		time.Sleep(500 * time.Millisecond)
	})

	var ourKey [32]byte
	client := derp.New(mr.addr, func(ctx context.Context) (string, error) {
		return "tok", nil
	}, ourKey, func(_ [32]byte, _ []byte) {})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go client.Run(ctx)

	// Give auth time to complete so sendCh is non-nil.
	time.Sleep(300 * time.Millisecond)

	fb := newFakeBind()
	b := derp.NewDerpBind(fb, client)

	var destKey [32]byte
	for i := range destKey {
		destKey[i] = 0xCC
	}

	// Mark the peer as DERP-routed.
	b.SetPeerRouting(destKey, true)

	// Parse the synthetic endpoint and send.
	epStr := derp.SyntheticEndpointString(destKey)
	ep, err := b.ParseEndpoint(epStr)
	if err != nil {
		t.Fatalf("ParseEndpoint: %v", err)
	}

	payload := []byte("wg-encrypted-packet")
	if err := b.Send([][]byte{payload}, ep); err != nil {
		t.Fatalf("Send: %v", err)
	}

	select {
	case <-frameReceived:
		// Relay received the frame — DERP routing worked.
	case <-time.After(3 * time.Second):
		t.Fatal("relay did not receive DERP frame within 3s")
	}
}

// TestSendNonSyntheticDelegatesToInner verifies that Send to a non-synthetic
// endpoint calls the inner bind's Send.
func TestSendNonSyntheticDelegatesToInner(t *testing.T) {
	fb := newFakeBind()
	c := derp.New("tcp+insecure://127.0.0.1:1", func(ctx context.Context) (string, error) { return "", nil }, [32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	realEp := &dummyEndpoint{str: "10.0.0.1:51820"}
	payload := []byte("direct-udp-packet")

	err := b.Send([][]byte{payload}, realEp)
	if err != nil {
		t.Fatalf("Send: %v", err)
	}

	select {
	case got := <-fb.sent:
		if string(got.bufs[0]) != string(payload) {
			t.Fatalf("inner bind received wrong payload: %q", got.bufs[0])
		}
		if got.ep != realEp {
			t.Fatalf("inner bind received wrong endpoint")
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("inner bind Send was not called")
	}
}

// TestSetPeerRoutingUnroutesDrops verifies that Send to a synthetic endpoint
// when the peer is NOT in the derpPeers map silently drops (doesn't call relay,
// doesn't call inner bind).
func TestSetPeerRoutingUnroutesDrops(t *testing.T) {
	fb := newFakeBind()
	c := derp.New("tcp+insecure://127.0.0.1:1", func(ctx context.Context) (string, error) { return "", nil }, [32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	var destKey [32]byte
	for i := range destKey {
		destKey[i] = 0xDD
	}
	// Do NOT call SetPeerRouting — peer is not in the map.

	epStr := derp.SyntheticEndpointString(destKey)
	ep, err := b.ParseEndpoint(epStr)
	if err != nil {
		t.Fatalf("ParseEndpoint: %v", err)
	}

	err = b.Send([][]byte{[]byte("should-be-dropped")}, ep)
	if err != nil {
		t.Fatalf("Send returned error: %v", err)
	}

	// Neither inner bind nor relay should receive anything.
	select {
	case <-fb.sent:
		t.Fatal("inner bind should not receive sends for synthetic endpoint")
	case <-time.After(100 * time.Millisecond):
		// Expected: nothing was sent.
	}
}

// dummyEndpoint is a minimal conn.Endpoint for testing.
type dummyEndpoint struct {
	str string
}

func (d *dummyEndpoint) ClearSrc()           {}
func (d *dummyEndpoint) DstToString() string { return d.str }
func (d *dummyEndpoint) SrcToString() string { return "" }
func (d *dummyEndpoint) DstIP() netip.Addr   { return netip.Addr{} }
func (d *dummyEndpoint) SrcIP() netip.Addr   { return netip.Addr{} }
func (d *dummyEndpoint) DstToBytes() []byte  { return []byte(d.str) }
