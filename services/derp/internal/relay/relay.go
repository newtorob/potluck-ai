// Package relay implements the Potluck DERP relay server. The name follows
// Tailscale's DERP (Designated Encrypted Relay for Packets).
//
// This is a simple Potluck-specific protocol, not Tailscale's full DERP wire
// format. The design is intentionally minimal and auditable.
//
// Wire format (after auth):
//
//	Client → Server (forward to peer):
//	  [2-byte big-endian payload-length][32-byte dest-wg-pubkey][payload]
//
//	Server → Client (deliver from peer):
//	  [2-byte big-endian payload-length][32-byte source-wg-pubkey][payload]
//
// The 2-byte length is the total payload length (not including the 32-byte
// pubkey prefix). Frames are capped at 65535 bytes which comfortably covers
// any WireGuard packet (max MTU 1500).
//
// Auth handshake:
//  1. Client sends JSON line: {"type":"auth","token":"<JWT>"}
//  2. Server validates JWT claims (wg_pubkey, region, exp).
//  3. Server replies:  {"type":"auth_ok","region":"<region>","server_time":<unix>}
//
// Privacy:
//   - Never log payload contents.
//   - Never log full pubkeys - truncate to 8 hex chars for log correlation.
//   - Aggregate counters (bytes forwarded, client count) are fine.
package relay

import (
	"bufio"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const (
	// pubkeyLen is the raw WireGuard public key length in bytes.
	pubkeyLen = 32

	// maxPayloadLen is the maximum per-frame payload size.
	maxPayloadLen = 65535

	// authTimeout is how long we wait for the client's first auth message.
	authTimeout = 15 * time.Second

	// maxPacketsPerSecond is the per-client rate limit (token bucket capacity).
	// Exceeding this drops the packet silently; does not kick the client.
	maxPacketsPerSecond = 1000

	// tokenBucketRefillInterval is how often the bucket is refilled to capacity.
	tokenBucketRefillInterval = time.Second
)

// authMsg is the JSON structure the client sends as its first message.
type authMsg struct {
	Type  string `json:"type"`
	Token string `json:"token"`
}

// authOKMsg is sent back to the client after successful auth.
type authOKMsg struct {
	Type       string `json:"type"`
	Region     string `json:"region"`
	ServerTime int64  `json:"server_time"`
}

// derpClaims are the JWT claims we expect in the DERP auth token.
type derpClaims struct {
	jwt.RegisteredClaims
	WGPubkey string `json:"wg_pubkey"`
	Region   string `json:"region"`
}

// client represents one authenticated, connected DERP peer.
type client struct {
	conn      net.Conn
	pubkey    [pubkeyLen]byte // raw 32-byte WG pubkey
	pubkeyHex string          // lower-hex encoding, used as map key

	writeMu sync.Mutex // serialises all writes to conn

	// token-bucket rate limiter fields
	bucketMu   sync.Mutex
	tokens     int64
	lastRefill time.Time
}

// tryConsume attempts to consume one rate-limit token.
// Returns true if the packet is allowed, false if it should be dropped.
func (c *client) tryConsume() bool {
	c.bucketMu.Lock()
	defer c.bucketMu.Unlock()

	now := time.Now()
	if now.Sub(c.lastRefill) >= tokenBucketRefillInterval {
		c.tokens = maxPacketsPerSecond
		c.lastRefill = now
	}
	if c.tokens <= 0 {
		return false
	}
	c.tokens--
	return true
}

// sendFrame writes one delivery frame to the client.
// Frame: [uint16 payload-len][32-byte source-pubkey][payload]
// Must not be called concurrently - writeMu is held by the caller.
func (c *client) sendFrame(sourcePubkey [pubkeyLen]byte, payload []byte) error {
	if len(payload) > maxPayloadLen {
		return fmt.Errorf("payload %d bytes exceeds max %d", len(payload), maxPayloadLen)
	}
	frame := make([]byte, 2+pubkeyLen+len(payload))
	binary.BigEndian.PutUint16(frame[0:2], uint16(len(payload)))
	copy(frame[2:2+pubkeyLen], sourcePubkey[:])
	copy(frame[2+pubkeyLen:], payload)

	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	_, err := c.conn.Write(frame)
	return err
}

// Relay is the DERP relay server.
type Relay struct {
	region   string
	jwtKey   []byte
	maxConns int

	mu      sync.RWMutex
	clients map[string]*client // pubkeyHex → *client

	// atomic metrics
	totalBytesForwarded atomic.Int64
	connectedClients    atomic.Int32
}

// Config holds Relay configuration.
type Config struct {
	// Region is the DERP region identifier (e.g. "ord").
	Region string
	// JWTKey is the HMAC-SHA256 signing key shared with the coordinator.
	JWTKey []byte
	// MaxClients is the maximum number of concurrent connections (default 10 000).
	MaxClients int
}

// New creates a Relay. Call HandleConn for each accepted TCP connection.
func New(cfg Config) *Relay {
	maxConns := cfg.MaxClients
	if maxConns <= 0 {
		maxConns = 10_000
	}
	return &Relay{
		region:   cfg.Region,
		jwtKey:   cfg.JWTKey,
		maxConns: maxConns,
		clients:  make(map[string]*client, 64),
	}
}

// ConnectedClients returns the current number of authenticated clients.
func (r *Relay) ConnectedClients() int {
	return int(r.connectedClients.Load())
}

// TotalBytesForwarded returns cumulative bytes forwarded since startup.
func (r *Relay) TotalBytesForwarded() int64 {
	return r.totalBytesForwarded.Load()
}

// HandleConn handles one incoming TCP connection. Must be called in its own
// goroutine; it returns when the connection closes.
func (r *Relay) HandleConn(conn net.Conn) {
	defer conn.Close()

	// ---- auth phase ---- //
	cli, err := r.authenticate(conn)
	if err != nil {
		// Write a plain JSON error - we can't use the frame format before auth.
		_, _ = fmt.Fprintf(conn, `{"type":"auth_error","message":%q}`+"\n", err.Error())
		slog.Warn("derp: auth failed",
			"remote", conn.RemoteAddr().String(),
			"err", err,
		)
		return
	}

	// ---- capacity check + register ---- //
	r.mu.Lock()
	if len(r.clients) >= r.maxConns {
		r.mu.Unlock()
		_, _ = fmt.Fprintf(conn, `{"type":"auth_error","message":"relay at capacity"}`+"\n")
		slog.Warn("derp: rejected connection, at capacity",
			"pubkey_prefix", cli.pubkeyHex[:8],
		)
		return
	}
	// Replace any stale connection for this pubkey (reconnect scenario).
	if old, ok := r.clients[cli.pubkeyHex]; ok {
		old.conn.Close()
	}
	r.clients[cli.pubkeyHex] = cli
	r.mu.Unlock()

	r.connectedClients.Add(1)
	slog.Info("derp: client connected",
		"pubkey_prefix", cli.pubkeyHex[:8],
		"region", r.region,
	)

	// ---- packet forwarding loop ---- //
	r.readLoop(cli)

	// ---- deregister ---- //
	r.mu.Lock()
	if cur := r.clients[cli.pubkeyHex]; cur == cli {
		delete(r.clients, cli.pubkeyHex)
	}
	r.mu.Unlock()
	r.connectedClients.Add(-1)

	slog.Info("derp: client disconnected", "pubkey_prefix", cli.pubkeyHex[:8])
}

// authenticate reads the auth JSON line, validates the JWT, writes auth_ok,
// and returns an authenticated *client. Returns non-nil error on any failure.
func (r *Relay) authenticate(conn net.Conn) (*client, error) {
	if err := conn.SetDeadline(time.Now().Add(authTimeout)); err != nil {
		return nil, fmt.Errorf("set deadline: %w", err)
	}
	// Remove the deadline after auth so the data phase has no timeout.
	defer func() { _ = conn.SetDeadline(time.Time{}) }() // clear auth deadline after auth completes

	scanner := bufio.NewScanner(conn)
	if !scanner.Scan() {
		if err := scanner.Err(); err != nil {
			return nil, fmt.Errorf("read auth line: %w", err)
		}
		return nil, fmt.Errorf("read auth line: %w", io.EOF)
	}

	var msg authMsg
	if err := json.Unmarshal(scanner.Bytes(), &msg); err != nil {
		return nil, fmt.Errorf("parse auth JSON: %w", err)
	}
	if msg.Type != "auth" {
		return nil, fmt.Errorf("expected type=auth, got %q", msg.Type)
	}
	if msg.Token == "" {
		return nil, errors.New("empty auth token")
	}

	// Parse + validate the JWT.
	token, err := jwt.ParseWithClaims(
		msg.Token,
		&derpClaims{},
		func(t *jwt.Token) (interface{}, error) {
			if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
				return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
			}
			return r.jwtKey, nil
		},
	)
	if err != nil {
		return nil, fmt.Errorf("JWT validation: %w", err)
	}

	claims, ok := token.Claims.(*derpClaims)
	if !ok || !token.Valid {
		return nil, errors.New("JWT claims invalid")
	}
	if claims.Region != r.region {
		return nil, fmt.Errorf("JWT region mismatch: got %q, want %q", claims.Region, r.region)
	}

	// Decode the 32-byte WG public key from the JWT claim (standard base64).
	keyBytes, err := base64.StdEncoding.DecodeString(claims.WGPubkey)
	if err != nil {
		// Try URL-safe base64 as a fallback (Python's PyJWT defaults vary).
		keyBytes, err = base64.RawStdEncoding.DecodeString(claims.WGPubkey)
		if err != nil {
			return nil, fmt.Errorf("decode wg_pubkey from JWT: %w", err)
		}
	}
	if len(keyBytes) != pubkeyLen {
		return nil, fmt.Errorf("wg_pubkey must be 32 bytes, got %d", len(keyBytes))
	}

	var pubkeyRaw [pubkeyLen]byte
	copy(pubkeyRaw[:], keyBytes)
	pubkeyHex := hex.EncodeToString(pubkeyRaw[:])

	// Send auth_ok.
	resp, _ := json.Marshal(authOKMsg{
		Type:       "auth_ok",
		Region:     r.region,
		ServerTime: time.Now().Unix(),
	})
	resp = append(resp, '\n')
	if _, err := conn.Write(resp); err != nil {
		return nil, fmt.Errorf("write auth_ok: %w", err)
	}

	return &client{
		conn:       conn,
		pubkey:     pubkeyRaw,
		pubkeyHex:  pubkeyHex,
		tokens:     maxPacketsPerSecond,
		lastRefill: time.Now(),
	}, nil
}

// readLoop reads frames from an authenticated client and forwards them.
// Frame shape client→server: [uint16 payload-len][32-byte dest-pubkey][payload]
func (r *Relay) readLoop(cli *client) {
	var lenBuf [2]byte
	var destBuf [pubkeyLen]byte

	for {
		// 2-byte length.
		if _, err := io.ReadFull(cli.conn, lenBuf[:]); err != nil {
			if !isClosedErr(err) {
				slog.Debug("derp: read length",
					"pubkey_prefix", cli.pubkeyHex[:8],
					"err", err,
				)
			}
			return
		}
		payloadLen := int(binary.BigEndian.Uint16(lenBuf[:]))
		if payloadLen == 0 {
			// Keepalive / heartbeat - no dest pubkey follows. Ignore.
			continue
		}

		// 32-byte destination pubkey.
		if _, err := io.ReadFull(cli.conn, destBuf[:]); err != nil {
			if !isClosedErr(err) {
				slog.Debug("derp: read dest pubkey",
					"pubkey_prefix", cli.pubkeyHex[:8],
					"err", err,
				)
			}
			return
		}

		// Payload.
		payload := make([]byte, payloadLen)
		if _, err := io.ReadFull(cli.conn, payload); err != nil {
			if !isClosedErr(err) {
				slog.Debug("derp: read payload",
					"pubkey_prefix", cli.pubkeyHex[:8],
					"err", err,
				)
			}
			return
		}

		// Rate-limit: drop silently if over quota.
		if !cli.tryConsume() {
			slog.Debug("derp: packet rate-limited",
				"pubkey_prefix", cli.pubkeyHex[:8],
			)
			continue
		}

		r.forward(cli.pubkey, destBuf, payload)
	}
}

// forward looks up destPubkey in the client map and writes the packet.
// If the destination is not connected, the packet is silently dropped to
// avoid leaking whether a pubkey is currently registered (side-channel).
func (r *Relay) forward(srcPubkey, destPubkey [pubkeyLen]byte, payload []byte) {
	destHex := hex.EncodeToString(destPubkey[:])

	r.mu.RLock()
	dest, ok := r.clients[destHex]
	r.mu.RUnlock()

	if !ok {
		return // silent drop
	}

	if err := dest.sendFrame(srcPubkey, payload); err != nil {
		// Dest connection broken; its readLoop goroutine will clean up.
		slog.Debug("derp: forward write failed",
			"dest_prefix", destHex[:8],
			"err", err,
		)
		return
	}

	r.totalBytesForwarded.Add(int64(len(payload)))
}

// Shutdown closes all client connections. Called on SIGTERM.
func (r *Relay) Shutdown() {
	r.mu.Lock()
	defer r.mu.Unlock()

	slog.Info("derp: shutting down", "client_count", len(r.clients))
	for _, cli := range r.clients {
		_, _ = fmt.Fprintf(cli.conn, `{"type":"server_shutdown"}`+"\n")
		cli.conn.Close()
	}
	r.clients = make(map[string]*client)
}

// isClosedErr returns true if err represents a normal closed-connection
// condition that doesn't warrant a warning log.
func isClosedErr(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
		return true
	}
	var ne *net.OpError
	return errors.As(err, &ne)
}
