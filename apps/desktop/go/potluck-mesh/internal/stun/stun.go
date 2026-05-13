// Package stun provides a minimal STUN client that resolves this node's
// external UDP endpoint (ip:port) using the RFC 8489 binding request.
//
// Design choices:
//   - IPv6-first: tries UDP6 first; falls back to UDP4 if v6 is unavailable or
//     returns no address (common on IPv4-only or ICMPv6-filtered links).
//   - No raw-response logging: the full STUN binding response is never logged
//     (it can contain server-side metadata we don't want to surface).
//   - Errors back off and retry; they never panic.
//   - Multiple server addresses are tried in order; the first success wins.
//
// Privacy: this package only handles external ip:port strings, which are
// already considered public mesh-layer metadata (the coordinator stores them
// in mesh_nodes.last_endpoint and broadcasts them to household peers).
package stun

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"time"

	"github.com/pion/stun/v2"
)

const (
	// dialTimeout is the per-attempt network deadline for a single STUN
	// binding request. Keep it short so the fallback to v4 is fast.
	dialTimeout = 3 * time.Second
)

// DefaultServers is the fallback STUN server list when none are configured.
// Cloudflare first (lower latency, privacy-forward operator), Google second.
var DefaultServers = []string{
	"stun.cloudflare.com:3478",
	"stun.l.google.com:19302",
}

// Resolver performs STUN binding requests to discover the external endpoint.
type Resolver struct {
	servers []string // "host:port" strings
}

// NewResolver creates a Resolver. If servers is empty, DefaultServers is used.
func NewResolver(servers []string) *Resolver {
	if len(servers) == 0 {
		servers = DefaultServers
	}
	return &Resolver{servers: servers}
}

// Resolve performs a STUN binding request and returns the external endpoint
// as "ip:port". It tries UDP6 first, then UDP4 as a fallback.
// All configured servers are tried in order before giving up.
//
// The context controls the overall deadline. Individual attempts use
// dialTimeout per network + server combination.
func (r *Resolver) Resolve(ctx context.Context) (string, error) {
	var lastErr error

	for _, server := range r.servers {
		// IPv6 first.
		if ep, err := r.tryResolve(ctx, "udp6", server); err == nil {
			return ep, nil
		} else {
			slog.Debug("stun: v6 attempt failed, trying v4",
				"server", server,
				"err", err,
			)
			lastErr = err
		}

		// IPv4 fallback.
		if ep, err := r.tryResolve(ctx, "udp4", server); err == nil {
			return ep, nil
		} else {
			slog.Debug("stun: v4 attempt failed",
				"server", server,
				"err", err,
			)
			lastErr = err
		}

		if ctx.Err() != nil {
			return "", ctx.Err()
		}
	}

	return "", fmt.Errorf("stun: all servers exhausted: %w", lastErr)
}

// tryResolve performs a single STUN binding request over network ("udp4" or
// "udp6") to the given server address. Returns the mapped address as "ip:port".
func (r *Resolver) tryResolve(ctx context.Context, network, server string) (string, error) {
	// Resolve the server address for the requested network family.
	// net.ResolveUDPAddr handles both raw IPs and hostnames.
	serverAddr, err := net.ResolveUDPAddr(network, server)
	if err != nil {
		return "", fmt.Errorf("resolve server addr (%s %s): %w", network, server, err)
	}

	// Bind a local UDP socket. Port 0 → OS picks an ephemeral port.
	// We need a bound socket so we can learn the mapped external port.
	// nil IP lets the OS pick the appropriate unspecified address for the
	// network family (0.0.0.0 for udp4, :: for udp6).
	conn, err := net.ListenUDP(network, &net.UDPAddr{})
	if err != nil {
		return "", fmt.Errorf("bind local UDP (%s): %w", network, err)
	}
	defer conn.Close() // UDP socket; close error on defer is non-actionable

	// Honour the context + per-attempt dial timeout (whichever comes first).
	deadline := time.Now().Add(dialTimeout)
	if ctxDeadline, ok := ctx.Deadline(); ok && ctxDeadline.Before(deadline) {
		deadline = ctxDeadline
	}
	if err := conn.SetDeadline(deadline); err != nil {
		return "", fmt.Errorf("set deadline: %w", err)
	}

	// Build and send the STUN binding request.
	msg, err := stun.Build(stun.TransactionID, stun.BindingRequest)
	if err != nil {
		return "", fmt.Errorf("build stun message: %w", err)
	}

	if _, err := conn.WriteTo(msg.Raw, serverAddr); err != nil {
		return "", fmt.Errorf("send stun request: %w", err)
	}

	// Read the response. Allocate a modest buffer; STUN responses are small.
	buf := make([]byte, 1024)
	n, _, err := conn.ReadFromUDP(buf)
	if err != nil {
		return "", fmt.Errorf("read stun response: %w", err)
	}

	// Parse the response — intentionally do NOT log raw bytes.
	var resp stun.Message
	resp.Raw = buf[:n]
	if err := resp.Decode(); err != nil {
		return "", fmt.Errorf("decode stun response: %w", err)
	}

	// Extract the XOR-MAPPED-ADDRESS attribute (RFC 8489 §14.2).
	// Fall back to MAPPED-ADDRESS if XOR variant is absent.
	var xorAddr stun.XORMappedAddress
	if err := xorAddr.GetFrom(&resp); err == nil {
		return formatEndpoint(xorAddr.IP, xorAddr.Port), nil
	}

	var mappedAddr stun.MappedAddress
	if err := mappedAddr.GetFrom(&resp); err == nil {
		return formatEndpoint(mappedAddr.IP, mappedAddr.Port), nil
	}

	return "", fmt.Errorf("stun response contained no address attribute")
}

// formatEndpoint returns "ip:port". For IPv6, the IP is wrapped in brackets
// so the result is unambiguous: "[::1]:port".
func formatEndpoint(ip net.IP, port int) string {
	if ip.To4() == nil {
		// IPv6 — use bracket notation.
		return fmt.Sprintf("[%s]:%d", ip.String(), port)
	}
	return fmt.Sprintf("%s:%d", ip.String(), port)
}
