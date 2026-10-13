// Package derp provides a client for the Potluck DERP relay protocol.
//
// The DERP relay is a fallback path for WireGuard packets when direct
// UDP hole-punch fails (symmetric NAT, restrictive corporate firewalls).
// See "Path selection" in docs/phase-g/mesh-design.md.
//
// Wire format (relay → client delivery):
//
//	[2-byte big-endian payload-length][32-byte source-wg-pubkey][payload]
//
// Wire format (client → relay forwarding):
//
//	[2-byte big-endian payload-length][32-byte dest-wg-pubkey][payload]
//
// Auth handshake (first thing after TCP connect):
//  1. Client sends: JSON line {"type":"auth","token":"<JWT>"}
//  2. Server replies: {"type":"auth_ok","region":"...","server_time":<unix>}
//
// Concurrency:
//   - sendCh is an atomic.Pointer[chan sendItem]; runOnce Stores a fresh
//     channel after auth, and Stores nil only AFTER its read/write loop
//     goroutines have exited (sync.WaitGroup). Callers (Send) Load the
//     pointer - a nil load means "no active connection, drop." Senders
//     use select-default so a full buffer never blocks; sending to an
//     orphaned channel is safe because nothing close()s it.
//   - One read-loop goroutine; it calls onPacketRx for each inbound frame.
//   - One write-loop goroutine; it drains the sendCh channel.
//     The WaitGroup pairing guarantees both have exited before the next
//     reconnect arms a new channel.
//
// Privacy:
//   - Never log payload bytes.
//   - Truncate pubkeys to 8 chars in logs.
package derp

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/util"
)

const (
	// pubkeyLen is the raw WireGuard public key length in bytes.
	pubkeyLen = 32

	// maxPayloadLen is the maximum frame payload (16-bit cap).
	maxPayloadLen = 65535

	// sendChanCap is the outbound send channel buffer size.
	// Match signal/client.go - deep enough to absorb bursts.
	sendChanCap = 16

	// authTimeout is how long we wait for the relay's auth_ok response.
	authTimeout = 15 * time.Second

	// initialBackoff is the first reconnect delay on disconnect.
	initialBackoff = 500 * time.Millisecond

	// maxBackoff caps exponential backoff.
	maxBackoff = 60 * time.Second

	// jwtRefreshInterval is how often we proactively refresh the JWT.
	// Coordinator issues 1-hour tokens; refresh at 50 minutes so there
	// is a 10-minute buffer before expiry. The fetcher is also called on
	// every reconnect regardless of this timer.
	jwtRefreshInterval = 50 * time.Minute
)

// PacketRxHandler is called (on the read-loop goroutine) for every inbound
// packet delivered by the relay. Implementations must not block - dispatch
// to a channel or goroutine if non-trivial work is needed.
type PacketRxHandler func(sourcePubkey [pubkeyLen]byte, payload []byte)

// JWTFetcher is a function that returns a fresh JWT for the DERP relay.
// Called on every reconnect and periodically by the refresh goroutine.
type JWTFetcher func(ctx context.Context) (string, error)

// sendItem is one outbound frame queued for the writer goroutine.
type sendItem struct {
	destPubkey [pubkeyLen]byte
	payload    []byte
}

// Client is a persistent connection to a single DERP relay endpoint.
// It reconnects automatically with exponential backoff on any failure.
//
// Create with New and call Run(ctx) to start. Remains usable through
// reconnects - Send() queues to the channel and the writer handles timing.
type Client struct {
	relayURL   string
	jwtFetcher JWTFetcher
	ourPubkey  [pubkeyLen]byte
	onPacketRx PacketRxHandler

	// sendCh is the outbound queue. Stored as an atomic.Pointer so concurrent
	// Send() callers can Load it without racing against runOnce installing a
	// fresh channel on reconnect or clearing it after teardown.
	sendCh atomic.Pointer[chan sendItem]

	// currentJWT caches the most recently fetched token.
	// jwtMu must be held for any read or write: jwtRefreshLoop runs concurrently
	// with runOnce, so an unguarded string assignment is a two-word write-write
	// race under the Go memory model.
	jwtMu      sync.Mutex
	currentJWT string
}

// loadSendCh returns the currently armed send channel, or nil if no
// connection is active.
func (c *Client) loadSendCh() chan sendItem {
	p := c.sendCh.Load()
	if p == nil {
		return nil
	}
	return *p
}

// New creates a Client. Call Run(ctx) to start the connection loop.
//
//   - relayURL: "tcp://host:port" or "tcp+insecure://host:port" (dev cleartext)
//   - jwtFetcher: called on each connection attempt to get a fresh auth token
//   - ourPubkey: this node's raw 32-byte WireGuard public key
//   - onPacketRx: called for each inbound packet from the relay
func New(relayURL string, jwtFetcher JWTFetcher, ourPubkey [pubkeyLen]byte, onPacketRx PacketRxHandler) *Client {
	return &Client{
		relayURL:   relayURL,
		jwtFetcher: jwtFetcher,
		ourPubkey:  ourPubkey,
		onPacketRx: onPacketRx,
	}
}

// Send enqueues a packet for delivery to destPubkey via the relay.
// Concurrency-safe. Returns an error if the channel is full (caller can retry).
// If there is no active connection, the send is dropped with a debug log.
func (c *Client) Send(destPubkey [pubkeyLen]byte, payload []byte) error {
	ch := c.loadSendCh()
	if ch == nil {
		slog.Debug("derp: no active connection, dropping outbound packet",
			"dest_prefix", util.TruncatePubkeyBytes(destPubkey),
		)
		return fmt.Errorf("derp: no active connection")
	}
	select {
	case ch <- sendItem{destPubkey: destPubkey, payload: payload}:
		return nil
	default:
		slog.Warn("derp: send channel full, dropping outbound packet",
			"dest_prefix", util.TruncatePubkeyBytes(destPubkey),
		)
		return fmt.Errorf("derp: send channel full")
	}
}

// Run connects to the DERP relay and maintains the connection until ctx is
// cancelled. Reconnects with exponential backoff on any disconnect.
// Also starts a background goroutine that refreshes the JWT proactively.
//
// Blocks until ctx is cancelled. Returns ctx.Err() when done.
func (c *Client) Run(ctx context.Context) error {
	// Background JWT refresh - keeps the token fresh so each reconnect
	// immediately gets a valid token rather than having to wait for fetch.
	go c.jwtRefreshLoop(ctx)

	backoff := initialBackoff
	attempt := 0

	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		attempt++
		slog.Info("derp: connecting", "url", c.relayURL, "attempt", attempt)

		established, err := c.runOnce(ctx)
		// runOnce clears sendCh AFTER its read/write loop goroutines have
		// exited (WaitGroup ordering), so Send() reliably observes nil
		// before any subsequent reconnect arms a new channel.

		if ctx.Err() != nil {
			return ctx.Err()
		}

		if established {
			backoff = initialBackoff // reset after a real connection
		}

		if err != nil {
			slog.Warn("derp: disconnected, will reconnect",
				"attempt", attempt,
				"backoff_ms", backoff.Milliseconds(),
				"err", err,
			)
		} else {
			slog.Info("derp: connection closed cleanly, reconnecting",
				"attempt", attempt,
				"backoff_ms", backoff.Milliseconds(),
			)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}

		// Exponential backoff: 500ms → 1s → 2s → … → 60s.
		backoff = time.Duration(math.Min(float64(backoff*2), float64(maxBackoff)))
	}
}

// runOnce establishes a single connection to the relay, performs auth, and
// drives the read/write loops until the connection closes.
//
// Returns (true, err) if auth_ok was received (connection was established),
// or (false, err) if the connection never got past auth.
func (c *Client) runOnce(ctx context.Context) (established bool, _ error) {
	// Fetch a fresh JWT for this connection attempt.
	jwt, err := c.jwtFetcher(ctx)
	if err != nil {
		return false, fmt.Errorf("derp: fetch JWT: %w", err)
	}
	c.jwtMu.Lock()
	c.currentJWT = jwt
	c.jwtMu.Unlock()

	// Dial the relay.
	conn, err := c.dial(ctx)
	if err != nil {
		return false, fmt.Errorf("derp: dial: %w", err)
	}
	defer conn.Close()

	// ---- send auth ---- //
	authLine, _ := json.Marshal(map[string]string{
		"type":  "auth",
		"token": jwt,
	})
	authLine = append(authLine, '\n')
	if _, err := conn.Write(authLine); err != nil {
		return false, fmt.Errorf("derp: send auth: %w", err)
	}

	// ---- wait for auth_ok ---- //
	if err := conn.SetDeadline(time.Now().Add(authTimeout)); err != nil {
		return false, fmt.Errorf("derp: set deadline: %w", err)
	}
	// Use bufio.Reader (NOT bufio.Scanner) so any bytes buffered past the
	// auth_ok newline - typically the first binary delivery frame, which
	// the relay can write immediately after auth_ok - are preserved and
	// readable by the binary readLoop below. Scanner.Bytes() returns the
	// line but the underlying scan-buffer state is opaque and over-reads;
	// a shared bufio.Reader keeps everything in one place.
	br := bufio.NewReader(conn)
	authLine, authErr := br.ReadBytes('\n')
	if authErr != nil {
		return false, fmt.Errorf("derp: read auth_ok: %w", authErr)
	}
	_ = conn.SetDeadline(time.Time{}) // clear auth deadline; errors here are non-fatal

	var resp map[string]interface{}
	if err := json.Unmarshal(authLine, &resp); err != nil {
		return false, fmt.Errorf("derp: parse auth_ok: %w", err)
	}
	if typ, _ := resp["type"].(string); typ != "auth_ok" {
		return false, fmt.Errorf("derp: unexpected auth response type=%q", typ)
	}
	region, _ := resp["region"].(string)

	slog.Info("derp: auth_ok received",
		"region", region,
	)

	// Arm the outbound channel now that auth succeeded.
	sendCh := make(chan sendItem, sendChanCap)
	c.sendCh.Store(&sendCh)
	// Guarantee: clear the pointer only AFTER both loops have exited (we
	// Wait below). This prevents Send() from observing a stale channel
	// reference for a connection that has already finished tearing down.
	defer func() {
		c.sendCh.Store(nil)
	}()

	// ---- run read + write loops ---- //
	// loopCtx is cancelled when either loop exits or ctx is cancelled - this
	// is how we unblock writeLoop, which would otherwise wait forever on
	// sendCh after the underlying connection dies. The WaitGroup guarantees
	// runOnce does not return until both loops have exited, which in turn
	// guarantees the deferred Store(nil) above fires after teardown.
	loopCtx, cancelLoops := context.WithCancel(ctx)
	defer cancelLoops()

	var wg sync.WaitGroup
	wg.Add(2)

	readErr := make(chan error, 1)
	go func() {
		defer wg.Done()
		readErr <- c.readLoop(loopCtx, br)
	}()
	writeErr := make(chan error, 1)
	go func() {
		defer wg.Done()
		writeErr <- c.writeLoop(loopCtx, conn, sendCh)
	}()

	// finish cancels loopCtx (so writeLoop's select-on-ctx fires) and
	// closes the connection (so readLoop's blocking I/O returns), then
	// waits for both goroutines to actually exit.
	finish := func() {
		cancelLoops()
		_ = conn.Close()
		wg.Wait()
	}

	select {
	case <-ctx.Done():
		finish()
		return true, ctx.Err()
	case err := <-readErr:
		finish()
		if err != nil {
			return true, fmt.Errorf("derp: read loop: %w", err)
		}
		return true, nil
	case err := <-writeErr:
		finish()
		if err != nil {
			return true, fmt.Errorf("derp: write loop: %w", err)
		}
		return true, nil
	}
}

// readLoop reads inbound delivery frames from the relay and dispatches them
// to onPacketRx. Frame shape: [uint16 len][32-byte src-pubkey][payload].
// Takes an io.Reader (rather than a net.Conn) so the caller can hand in the
// bufio.Reader that already buffered any frame bytes the relay sent
// immediately after auth_ok.
func (c *Client) readLoop(ctx context.Context, r io.Reader) error {
	var lenBuf [2]byte
	var srcBuf [pubkeyLen]byte

	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		// Length prefix.
		if _, err := io.ReadFull(r, lenBuf[:]); err != nil {
			return err
		}
		payloadLen := int(binary.BigEndian.Uint16(lenBuf[:]))
		if payloadLen == 0 {
			continue // keepalive
		}

		// Source pubkey.
		if _, err := io.ReadFull(r, srcBuf[:]); err != nil {
			return err
		}

		// Payload.
		payload := make([]byte, payloadLen)
		if _, err := io.ReadFull(r, payload); err != nil {
			return err
		}

		// Dispatch - must not block.
		// Privacy: never log payload.
		slog.Debug("derp: received packet",
			"src_prefix", util.TruncatePubkeyBytes(srcBuf),
			"payload_len", payloadLen,
		)
		c.onPacketRx(srcBuf, payload)
	}
}

// writeLoop drains sendCh and writes frames to the relay.
// Frame shape: [uint16 payload-len][32-byte dest-pubkey][payload].
func (c *Client) writeLoop(ctx context.Context, conn net.Conn, sendCh <-chan sendItem) error {
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case item, ok := <-sendCh:
			if !ok {
				return nil // channel closed
			}
			if err := c.writeFrame(conn, item.destPubkey, item.payload); err != nil {
				return fmt.Errorf("write frame: %w", err)
			}
		}
	}
}

// writeFrame encodes and writes one forwarding frame.
// Frame: [uint16 payload-len][32-byte dest-pubkey][payload]
func (c *Client) writeFrame(conn net.Conn, destPubkey [pubkeyLen]byte, payload []byte) error {
	if len(payload) > maxPayloadLen {
		return fmt.Errorf("payload %d bytes exceeds max %d", len(payload), maxPayloadLen)
	}
	frame := make([]byte, 2+pubkeyLen+len(payload))
	binary.BigEndian.PutUint16(frame[0:2], uint16(len(payload)))
	copy(frame[2:2+pubkeyLen], destPubkey[:])
	copy(frame[2+pubkeyLen:], payload)
	_, err := conn.Write(frame)
	return err
}

// dial opens a TCP connection to the relay. URL scheme controls TLS:
//   - "tcp+tls://host:port" - TLS (production)
//   - "tcp+insecure://host:port" - plain TCP, no TLS (dev / localhost)
//   - "tcp://host:port" - alias for tcp+insecure
func (c *Client) dial(ctx context.Context) (net.Conn, error) {
	url := c.relayURL
	useTLS := false

	var addr string
	switch {
	case strings.HasPrefix(url, "tcp+tls://"):
		addr = url[len("tcp+tls://"):]
		useTLS = true
	case strings.HasPrefix(url, "tcp+insecure://"):
		addr = url[len("tcp+insecure://"):]
	case strings.HasPrefix(url, "tcp://"):
		addr = url[len("tcp://"):]
	default:
		return nil, fmt.Errorf("derp: unsupported relay URL scheme: %q", url)
	}

	dialCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	if useTLS {
		// tls.DialWithDialer respects context cancellation via the dialer.
		tlsDialer := &tls.Dialer{
			NetDialer: &net.Dialer{},
			Config:    &tls.Config{MinVersion: tls.VersionTLS12},
		}
		return tlsDialer.DialContext(dialCtx, "tcp", addr)
	}

	var d net.Dialer
	return d.DialContext(dialCtx, "tcp", addr)
}

// jwtRefreshLoop proactively fetches a new JWT every jwtRefreshInterval.
// This keeps currentJWT fresh so a reconnect after a long idle period
// doesn't fail on a stale token.
func (c *Client) jwtRefreshLoop(ctx context.Context) {
	ticker := time.NewTicker(jwtRefreshInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			newJWT, err := c.jwtFetcher(ctx)
			if err != nil {
				slog.Warn("derp: proactive JWT refresh failed", "err", err)
				continue
			}
			c.jwtMu.Lock()
			c.currentJWT = newJWT
			c.jwtMu.Unlock()
			slog.Debug("derp: JWT refreshed proactively")
		}
	}
}
