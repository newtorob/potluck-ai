package relay

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"net"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// makeTestJWT creates a valid DERP auth JWT for testing.
func makeTestJWT(t *testing.T, key []byte, pubkeyB64 string, region string, expOffset time.Duration) string {
	t.Helper()
	now := time.Now()
	claims := derpClaims{
		RegisteredClaims: jwt.RegisteredClaims{
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(expOffset)),
		},
		WGPubkey: pubkeyB64,
		Region:   region,
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	signed, err := token.SignedString(key)
	if err != nil {
		t.Fatalf("makeTestJWT: %v", err)
	}
	return signed
}

// makeTestRelay creates a Relay and starts listening on a random local port.
// Returns the relay and the listener's address.
func makeTestRelay(t *testing.T, key []byte, region string) (*Relay, string) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	r := New(Config{Region: region, JWTKey: key})
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go r.HandleConn(conn)
		}
	}()
	t.Cleanup(func() {
		ln.Close()
		r.Shutdown()
	})
	return r, ln.Addr().String()
}

// dialAndAuth dials the relay, sends auth, and reads auth_ok.
// Returns the raw net.Conn for subsequent frame I/O.
func dialAndAuth(t *testing.T, addr, token string) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}

	// Send auth line.
	msg, _ := json.Marshal(authMsg{Type: "auth", Token: token})
	msg = append(msg, '\n')
	if _, err := conn.Write(msg); err != nil {
		t.Fatalf("write auth: %v", err)
	}

	// Read auth_ok (terminated by newline).
	var buf [512]byte
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second)) // test helper; deadline error is non-fatal
	n, err := conn.Read(buf[:])
	if err != nil {
		t.Fatalf("read auth_ok: %v", err)
	}
	_ = conn.SetDeadline(time.Time{}) // clear deadline

	var resp map[string]interface{}
	if err := json.Unmarshal(buf[:n], &resp); err != nil {
		t.Fatalf("parse auth_ok: %v (raw: %q)", err, buf[:n])
	}
	if typ, _ := resp["type"].(string); typ != "auth_ok" {
		t.Fatalf("expected auth_ok, got %q: %q", typ, buf[:n])
	}
	return conn
}

// sendFrame writes a client→server frame.
// Frame: [uint16 payload-len][32-byte dest-pubkey][payload]
func sendFrame(t *testing.T, conn net.Conn, destPubkey [32]byte, payload []byte) {
	t.Helper()
	frame := make([]byte, 2+32+len(payload))
	binary.BigEndian.PutUint16(frame[0:2], uint16(len(payload)))
	copy(frame[2:34], destPubkey[:])
	copy(frame[34:], payload)
	if _, err := conn.Write(frame); err != nil {
		t.Fatalf("sendFrame: %v", err)
	}
}

// recvFrame reads one server→client delivery frame from conn.
// Frame: [uint16 payload-len][32-byte source-pubkey][payload]
// Returns (srcPubkey, payload).
func recvFrame(t *testing.T, conn net.Conn, timeout time.Duration) ([32]byte, []byte) {
	t.Helper()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	defer func() { _ = conn.SetDeadline(time.Time{}) }()

	var lenBuf [2]byte
	if _, err := readFull(conn, lenBuf[:]); err != nil {
		t.Fatalf("recvFrame length: %v", err)
	}
	payloadLen := int(binary.BigEndian.Uint16(lenBuf[:]))

	var srcPubkey [32]byte
	if _, err := readFull(conn, srcPubkey[:]); err != nil {
		t.Fatalf("recvFrame src pubkey: %v", err)
	}
	payload := make([]byte, payloadLen)
	if _, err := readFull(conn, payload); err != nil {
		t.Fatalf("recvFrame payload: %v", err)
	}
	return srcPubkey, payload
}

func readFull(conn net.Conn, buf []byte) (int, error) {
	total := 0
	for total < len(buf) {
		n, err := conn.Read(buf[total:])
		total += n
		if err != nil {
			return total, err
		}
	}
	return total, nil
}

// TestForwarding verifies that a frame sent from client A is delivered to
// client B with the correct source pubkey and unchanged payload.
func TestForwarding(t *testing.T) {
	key := []byte("test-secret-key-32-bytes-exactly")
	region := "test"
	_, addr := makeTestRelay(t, key, region)

	// Generate two synthetic WG public keys (32 random-ish bytes).
	var keyA, keyB [32]byte
	for i := range keyA {
		keyA[i] = byte(0xAA + i)
	}
	for i := range keyB {
		keyB[i] = byte(0xBB + i)
	}
	keyAB64 := base64.StdEncoding.EncodeToString(keyA[:])
	keyBB64 := base64.StdEncoding.EncodeToString(keyB[:])

	// Connect both clients.
	tokenA := makeTestJWT(t, key, keyAB64, region, time.Hour)
	tokenB := makeTestJWT(t, key, keyBB64, region, time.Hour)
	connA := dialAndAuth(t, addr, tokenA)
	connB := dialAndAuth(t, addr, tokenB)
	defer connA.Close()
	defer connB.Close()

	// A sends a packet to B.
	want := []byte("hello from A to B")
	sendFrame(t, connA, keyB, want)

	// B should receive it with source = keyA.
	gotSrc, gotPayload := recvFrame(t, connB, 3*time.Second)
	if gotSrc != keyA {
		t.Errorf("source pubkey: got %x, want %x", gotSrc, keyA)
	}
	if string(gotPayload) != string(want) {
		t.Errorf("payload: got %q, want %q", gotPayload, want)
	}
}

// TestAuthRejectsExpiredJWT verifies that an expired JWT is rejected.
func TestAuthRejectsExpiredJWT(t *testing.T) {
	key := []byte("test-secret-key-32-bytes-exactly")
	region := "test"
	_, addr := makeTestRelay(t, key, region)

	var pubkey [32]byte
	pubkeyB64 := base64.StdEncoding.EncodeToString(pubkey[:])
	expiredToken := makeTestJWT(t, key, pubkeyB64, region, -time.Hour) // already expired

	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	msg, _ := json.Marshal(authMsg{Type: "auth", Token: expiredToken})
	msg = append(msg, '\n')
	_, _ = conn.Write(msg) // best-effort in test helper

	var buf [512]byte
	_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
	n, _ := conn.Read(buf[:])
	var resp map[string]interface{}
	_ = json.Unmarshal(buf[:n], &resp)
	if typ, _ := resp["type"].(string); typ != "auth_error" {
		t.Errorf("expected auth_error for expired JWT, got type=%q (raw: %q)", typ, buf[:n])
	}
}

// TestAuthRejectsWrongRegion verifies that a JWT for a different region is rejected.
func TestAuthRejectsWrongRegion(t *testing.T) {
	key := []byte("test-secret-key-32-bytes-exactly")
	_, addr := makeTestRelay(t, key, "ord")

	var pubkey [32]byte
	pubkeyB64 := base64.StdEncoding.EncodeToString(pubkey[:])
	wrongRegionToken := makeTestJWT(t, key, pubkeyB64, "dfw", time.Hour) // wrong region

	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	msg, _ := json.Marshal(authMsg{Type: "auth", Token: wrongRegionToken})
	msg = append(msg, '\n')
	_, _ = conn.Write(msg) // best-effort in test helper

	var buf [512]byte
	_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
	n, _ := conn.Read(buf[:])
	var resp map[string]interface{}
	_ = json.Unmarshal(buf[:n], &resp)
	if typ, _ := resp["type"].(string); typ != "auth_error" {
		t.Errorf("expected auth_error for wrong region, got type=%q", typ)
	}
}

// TestSilentDropUnknownDest verifies that a packet to an unconnected dest is
// silently dropped (no error reply to the sender).
func TestSilentDropUnknownDest(t *testing.T) {
	key := []byte("test-secret-key-32-bytes-exactly")
	region := "test"
	relay, addr := makeTestRelay(t, key, region)

	var keyA [32]byte
	for i := range keyA {
		keyA[i] = byte(0xAA + i)
	}
	tokenA := makeTestJWT(t, key, base64.StdEncoding.EncodeToString(keyA[:]), region, time.Hour)
	connA := dialAndAuth(t, addr, tokenA)
	defer connA.Close()

	// Send to a pubkey that is NOT connected.
	var unknownDest [32]byte
	for i := range unknownDest {
		unknownDest[i] = 0xFF
	}
	sendFrame(t, connA, unknownDest, []byte("goes nowhere"))

	// Give the relay time to process it.
	time.Sleep(100 * time.Millisecond)

	// The relay should not have crashed or sent anything back to connA.
	// Verify the relay is still functioning by checking client count.
	if got := relay.ConnectedClients(); got != 1 {
		t.Errorf("expected 1 connected client after silent drop, got %d", got)
	}
}

// TestMetrics verifies that forwarded bytes are counted.
func TestMetrics(t *testing.T) {
	key := []byte("test-secret-key-32-bytes-exactly")
	region := "test"
	relay, addr := makeTestRelay(t, key, region)

	var keyA, keyB [32]byte
	for i := range keyA {
		keyA[i] = byte(0x11 + i)
	}
	for i := range keyB {
		keyB[i] = byte(0x22 + i)
	}

	tokenA := makeTestJWT(t, key, base64.StdEncoding.EncodeToString(keyA[:]), region, time.Hour)
	tokenB := makeTestJWT(t, key, base64.StdEncoding.EncodeToString(keyB[:]), region, time.Hour)
	connA := dialAndAuth(t, addr, tokenA)
	connB := dialAndAuth(t, addr, tokenB)
	defer connA.Close()
	defer connB.Close()

	payload := []byte("counting bytes")
	sendFrame(t, connA, keyB, payload)
	recvFrame(t, connB, 2*time.Second)

	if got := relay.TotalBytesForwarded(); got != int64(len(payload)) {
		t.Errorf("TotalBytesForwarded: got %d, want %d", got, len(payload))
	}
}
