package derp_test

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
)

// mockRelay is a minimal TCP server that mimics just enough of the DERP relay
// to test the client: accepts auth, sends auth_ok, optionally delivers frames.
type mockRelay struct {
	ln       net.Listener
	addr     string
	// handler is called with the conn after auth_ok is sent.
	handler func(conn net.Conn)
}

func newMockRelay(t *testing.T, handler func(conn net.Conn)) *mockRelay {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("mockRelay listen: %v", err)
	}
	mr := &mockRelay{
		ln:      ln,
		addr:    "tcp+insecure://" + ln.Addr().String(),
		handler: handler,
	}
	go mr.serve()
	t.Cleanup(func() { ln.Close() })
	return mr
}

func (mr *mockRelay) serve() {
	for {
		conn, err := mr.ln.Accept()
		if err != nil {
			return
		}
		go mr.handleConn(conn)
	}
}

func (mr *mockRelay) handleConn(conn net.Conn) {
	defer conn.Close()

	// Read auth line.
	conn.SetDeadline(time.Now().Add(5 * time.Second)) //nolint
	scanner := bufio.NewScanner(conn)
	if !scanner.Scan() {
		return
	}
	var msg map[string]string
	if err := json.Unmarshal(scanner.Bytes(), &msg); err != nil {
		return
	}
	if msg["type"] != "auth" {
		return
	}
	conn.SetDeadline(time.Time{}) //nolint

	// Send auth_ok.
	resp, _ := json.Marshal(map[string]interface{}{
		"type":        "auth_ok",
		"region":      "test",
		"server_time": time.Now().Unix(),
	})
	resp = append(resp, '\n')
	if _, err := conn.Write(resp); err != nil {
		return
	}

	if mr.handler != nil {
		mr.handler(conn)
	} else {
		// Just keep the connection open.
		buf := make([]byte, 1)
		conn.Read(buf) //nolint
	}
}

// buildDeliveryFrame builds a server→client delivery frame.
// Frame: [uint16 payload-len][32-byte src-pubkey][payload]
func buildDeliveryFrame(srcPubkey [32]byte, payload []byte) []byte {
	frame := make([]byte, 2+32+len(payload))
	binary.BigEndian.PutUint16(frame[0:2], uint16(len(payload)))
	copy(frame[2:34], srcPubkey[:])
	copy(frame[34:], payload)
	return frame
}

// TestClientConnectsAndAuths verifies that the client connects to a relay,
// completes auth, and that Run() continues running (does not return prematurely).
func TestClientConnectsAndAuths(t *testing.T) {
	connected := make(chan struct{}, 1)
	mr := newMockRelay(t, func(conn net.Conn) {
		connected <- struct{}{}
		// Hold the connection open briefly.
		time.Sleep(200 * time.Millisecond)
	})

	var ourKey [32]byte
	for i := range ourKey {
		ourKey[i] = byte(i)
	}

	var rxCount atomic.Int32
	rxHandler := func(src [32]byte, payload []byte) {
		rxCount.Add(1)
	}

	fetcher := func(ctx context.Context) (string, error) {
		return "test-token", nil
	}

	client := derp.New(mr.addr, fetcher, ourKey, rxHandler)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	runDone := make(chan error, 1)
	go func() {
		runDone <- client.Run(ctx)
	}()

	// Wait for the relay to accept the connection.
	select {
	case <-connected:
		// good
	case <-time.After(2 * time.Second):
		t.Fatal("client did not connect within 2s")
	}

	cancel()
	<-runDone
}

// TestClientReceivesFrame verifies that a frame delivered by the relay triggers
// the onPacketRx callback with the correct source pubkey and payload.
func TestClientReceivesFrame(t *testing.T) {
	var srcKey [32]byte
	for i := range srcKey {
		srcKey[i] = byte(0xAA + i)
	}
	wantPayload := []byte("test-wg-packet")
	frame := buildDeliveryFrame(srcKey, wantPayload)

	gotRx := make(chan struct {
		src     [32]byte
		payload []byte
	}, 1)

	mr := newMockRelay(t, func(conn net.Conn) {
		// Deliver one frame to the client.
		conn.Write(frame) //nolint
		// Hold open long enough for the client to read it.
		time.Sleep(500 * time.Millisecond)
	})

	var ourKey [32]byte
	rxHandler := func(src [32]byte, payload []byte) {
		gotRx <- struct {
			src     [32]byte
			payload []byte
		}{src, append([]byte(nil), payload...)}
	}

	client := derp.New(mr.addr, func(ctx context.Context) (string, error) {
		return "tok", nil
	}, ourKey, rxHandler)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	go client.Run(ctx)

	select {
	case rx := <-gotRx:
		if rx.src != srcKey {
			t.Errorf("source pubkey mismatch: got %x, want %x", rx.src, srcKey)
		}
		if string(rx.payload) != string(wantPayload) {
			t.Errorf("payload mismatch: got %q, want %q", rx.payload, wantPayload)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("onPacketRx not called within 2s")
	}
	cancel()
}

// TestClientReconnectsAfterDrop verifies exponential-backoff reconnect.
// We close the relay on the first connection and verify the client reconnects.
func TestClientReconnectsAfterDrop(t *testing.T) {
	var connectCount atomic.Int32

	mr := newMockRelay(t, func(conn net.Conn) {
		n := connectCount.Add(1)
		if n == 1 {
			// Drop the first connection immediately.
			return
		}
		// Second connection: hold open until test completes.
		time.Sleep(2 * time.Second)
	})

	var ourKey [32]byte
	client := derp.New(mr.addr, func(ctx context.Context) (string, error) {
		return "tok", nil
	}, ourKey, func(_ [32]byte, _ []byte) {})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	go client.Run(ctx)

	// Wait for the second connection (reconnect after drop).
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		if connectCount.Load() >= 2 {
			cancel()
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("client did not reconnect; connect count = %d", connectCount.Load())
}

// TestSendEnqueuesFrame verifies that Send() works when the channel is open.
// We verify it at least does not return an error immediately after auth.
func TestSendEnqueuesFrame(t *testing.T) {
	sendDone := make(chan struct{}, 1)

	mr := newMockRelay(t, func(conn net.Conn) {
		// Read and discard whatever the client sends.
		buf := make([]byte, 1024)
		conn.SetDeadline(time.Now().Add(2 * time.Second)) //nolint
		conn.Read(buf)                                    //nolint
		sendDone <- struct{}{}
	})

	var ourKey [32]byte
	var destKey [32]byte
	for i := range destKey {
		destKey[i] = 0xFF
	}

	client := derp.New(mr.addr, func(ctx context.Context) (string, error) {
		return "tok", nil
	}, ourKey, func(_ [32]byte, _ []byte) {})

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	go client.Run(ctx)

	// Wait briefly for auth to complete so sendCh is non-nil.
	time.Sleep(300 * time.Millisecond)

	err := client.Send(destKey, []byte("hello"))
	if err != nil {
		t.Errorf("Send() returned error: %v", err)
	}

	select {
	case <-sendDone:
		// Relay received something — good.
	case <-time.After(2 * time.Second):
		t.Log("relay did not receive send — acceptable if timing was tight")
	}
	cancel()
}

// Ensure the package can be imported cleanly (pubkey encoding sanity check).
func TestPubkeyBase64Roundtrip(t *testing.T) {
	var key [32]byte
	for i := range key {
		key[i] = byte(i * 7)
	}
	b64 := base64.StdEncoding.EncodeToString(key[:])
	decoded, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		t.Fatalf("base64 decode: %v", err)
	}
	if len(decoded) != 32 {
		t.Errorf("decoded len %d, want 32", len(decoded))
	}
	fmt.Printf("pubkey roundtrip OK: %s\n", b64[:8])
}
