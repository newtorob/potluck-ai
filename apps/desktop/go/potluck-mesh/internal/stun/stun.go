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
//   - Probe socket bound to the WG transport port (magicsock-style): the STUN
//     probe socket is bound to the same local port the WireGuard data plane
//     uses (see bindProbeConn / NewResolver's localPort), not an OS-assigned
//     ephemeral one. On a per-flow/per-socket NAT (the common case for
//     consumer + CGNAT routers), the external mapping is otherwise unrelated
//     to the one WG traffic actually gets, which would point a hole-punch at
//     the wrong port. Falls back to an ephemeral port gracefully if the
//     reusable bind fails for any reason (including the WG socket already
//     owning the port on a platform where SO_REUSEPORT isn't available).
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
//
// NAT-type classification (ClassifyNAT) requires at least two distinct
// servers - this default list is chosen to satisfy that as well as the
// ordinary endpoint-resolution use.
var DefaultServers = []string{
	"stun.cloudflare.com:3478",
	"stun.l.google.com:19302",
}

// Resolver performs STUN binding requests to discover the external endpoint.
type Resolver struct {
	servers []string // "host:port" strings

	// localPort is the local UDP port every probe socket attempts to bind to
	// (see bindProbeConn). 0 disables the reusable-bind attempt entirely and
	// always uses an OS-assigned ephemeral port -
	// callers that don't care about NAT-mapping alignment with a specific
	// transport port (e.g. a test) can pass 0.
	localPort int
}

// NewResolver creates a Resolver. If servers is empty, DefaultServers is used.
//
// localPort is the local UDP port STUN probes attempt to bind to, so the
// resolved external endpoint reflects the same NAT mapping a UDP socket bound
// to that port (typically the WireGuard transport port) would get. Pass 0 to
// always use an OS-assigned ephemeral port instead (only appropriate when the resolved endpoint doesn't need to
// align with a specific local socket, e.g. in a unit test).
func NewResolver(servers []string, localPort int) *Resolver {
	if len(servers) == 0 {
		servers = DefaultServers
	}
	return &Resolver{servers: servers, localPort: localPort}
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
	conn, err := r.bindProbeConn(network)
	if err != nil {
		return "", fmt.Errorf("bind local UDP (%s): %w", network, err)
	}
	defer conn.Close() // UDP socket; close error on defer is non-actionable

	return r.probeOnce(ctx, conn, network, server)
}

// bindProbeConn opens the local UDP socket a STUN probe is sent from.
//
// When r.localPort is set (> 0), it first attempts a SO_REUSEADDR/SO_REUSEPORT
// bind to that exact port (see listenReusable, platform-specific) - this is
// what makes the STUN-observed mapping reflect the same NAT mapping the WG
// transport socket (already bound to that port) gets. If that fails for any
// reason (unsupported platform primitive, an unrelated bind error, or simply
// because the OS doesn't allow it), this logs at Debug and falls back to an
// OS-assigned ephemeral port - a probe on the wrong port is still strictly
// better than no probe at all (endpoint resolution / connectivity-check use
// cases degrade gracefully; only the NAT-mapping-alignment property is lost).
func (r *Resolver) bindProbeConn(network string) (*net.UDPConn, error) {
	if r.localPort > 0 {
		conn, err := listenReusable(network, r.localPort)
		if err == nil {
			return conn, nil
		}
		slog.Debug("stun: reusable bind to local port failed, falling back to ephemeral port",
			"network", network,
			"port", r.localPort,
			"err", err,
		)
	}
	// Port 0 → OS picks an ephemeral port. nil IP lets the OS pick the
	// appropriate unspecified address for the network family (0.0.0.0 for
	// udp4, :: for udp6).
	return net.ListenUDP(network, &net.UDPAddr{})
}

// probeOnce sends a single STUN binding request over the given (already
// bound) conn to server and returns the mapped external address as "ip:port".
// Shared by tryResolve (one probe per call) and ClassifyNAT (two probes over
// the same conn, to two different servers, so the comparison isolates
// per-destination vs. per-socket NAT mapping behaviour).
func (r *Resolver) probeOnce(ctx context.Context, conn *net.UDPConn, network, server string) (string, error) {
	// Resolve the server address for the requested network family.
	// net.ResolveUDPAddr handles both raw IPs and hostnames.
	serverAddr, err := net.ResolveUDPAddr(network, server)
	if err != nil {
		return "", fmt.Errorf("resolve server addr (%s %s): %w", network, server, err)
	}

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

	// Parse the response - intentionally do not log raw bytes.
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
		// IPv6 - use bracket notation.
		return fmt.Sprintf("[%s]:%d", ip.String(), port)
	}
	return fmt.Sprintf("%s:%d", ip.String(), port)
}

// ---------------------------------------------------------------------------
// NAT-type classification.
//
// A lightweight classifier for whether this node's own NAT allocates UDP
// mappings per-destination (symmetric) or per-socket (endpoint-independent /
// "cone"). Client-side hole-punching fundamentally cannot work against a
// symmetric NAT: the external ip:port a peer learns via the coordinator (from
// this node's STUN probe to server A) will not match the mapping the NAT
// actually uses when this node sends a packet to that peer's address, because
// the mapping depends on the destination. Classifying this up front lets
// OnPunchCoord skip a guaranteed-failure trial instead of retrying it forever.
// ---------------------------------------------------------------------------

// NATType is the result of ClassifyNAT.
type NATType int

const (
	// NATUnknown means classification did not complete (network error,
	// insufficient servers configured, etc.). Callers should fail open (treat
	// as "not symmetric") since an unclassified NAT is the common case before
	// the one-shot classifier completes at startup.
	NATUnknown NATType = iota
	// NATEndpointIndependent means the same external port was observed via
	// two different STUN servers from the same local port - a cone-family NAT
	// where client-side hole-punch is viable.
	NATEndpointIndependent
	// NATSymmetric means a different external port was observed per STUN
	// server - the NAT allocates mappings per-destination, so client-side
	// hole-punch cannot work reliably.
	NATSymmetric
)

func (n NATType) String() string {
	switch n {
	case NATEndpointIndependent:
		return "endpoint-independent"
	case NATSymmetric:
		return "symmetric"
	default:
		return "unknown"
	}
}

// ClassifyNAT performs STUN binding requests to at least two different
// configured servers, from the same local socket (and critically the same
// local port - see bindProbeConn), and compares the resulting external ports.
//
//   - Same external port on both → NATEndpointIndependent: the mapping is
//     keyed by local port only, independent of destination. Hole-punch is
//     viable (this is the majority of home/consumer NATs).
//   - Different external ports → NATSymmetric: the mapping is keyed by
//     (local port, destination), so the port a peer learns from this node's
//     STUN probe will not be the port this node's packets to that peer
//     actually arrive from. Hole-punch cannot work.
//
// Requires len(r.servers) >= 2; returns an error otherwise (a single-server
// configuration cannot distinguish the two cases).
//
// Tries "udp6" first, then "udp4" - mirroring Resolve's family preference -
// but never mixes families within a single classification attempt (comparing
// a v6 mapping against a v4 one would be meaningless).
func (r *Resolver) ClassifyNAT(ctx context.Context) (NATType, error) {
	if len(r.servers) < 2 {
		return NATUnknown, fmt.Errorf("stun: NAT classification requires >= 2 servers, have %d", len(r.servers))
	}

	var lastErr error
	for _, network := range []string{"udp6", "udp4"} {
		natType, err := r.classifyOnNetwork(ctx, network)
		if err == nil {
			return natType, nil
		}
		slog.Debug("stun: NAT classification attempt failed, trying next network family",
			"network", network,
			"err", err,
		)
		lastErr = err
		if ctx.Err() != nil {
			return NATUnknown, ctx.Err()
		}
	}
	return NATUnknown, fmt.Errorf("stun: NAT classification failed on all network families: %w", lastErr)
}

// classifyOnNetwork runs the two-probe comparison over a single network
// family, sharing one bound socket across both probes (required - see
// ClassifyNAT's doc for why using two different local ports would make the
// comparison meaningless).
func (r *Resolver) classifyOnNetwork(ctx context.Context, network string) (NATType, error) {
	conn, err := r.bindProbeConn(network)
	if err != nil {
		return NATUnknown, fmt.Errorf("bind local UDP (%s): %w", network, err)
	}
	defer conn.Close()

	ep1, err := r.probeOnce(ctx, conn, network, r.servers[0])
	if err != nil {
		return NATUnknown, fmt.Errorf("probe %s via %s: %w", network, r.servers[0], err)
	}
	ep2, err := r.probeOnce(ctx, conn, network, r.servers[1])
	if err != nil {
		return NATUnknown, fmt.Errorf("probe %s via %s: %w", network, r.servers[1], err)
	}

	port1, err := portOfEndpoint(ep1)
	if err != nil {
		return NATUnknown, fmt.Errorf("parse mapped port from %q: %w", r.servers[0], err)
	}
	port2, err := portOfEndpoint(ep2)
	if err != nil {
		return NATUnknown, fmt.Errorf("parse mapped port from %q: %w", r.servers[1], err)
	}

	if port1 != port2 {
		slog.Info("stun: NAT classified as symmetric (external port varies per destination)",
			"network", network,
			"port_via_server1", port1,
			"port_via_server2", port2,
		)
		return NATSymmetric, nil
	}
	slog.Debug("stun: NAT classified as endpoint-independent (same external port to both STUN servers)",
		"network", network,
		"external_port", port1,
	)
	return NATEndpointIndependent, nil
}

// portOfEndpoint extracts the numeric port from a "ip:port" or "[ip]:port"
// endpoint string as produced by formatEndpoint.
func portOfEndpoint(ep string) (int, error) {
	_, portStr, err := net.SplitHostPort(ep)
	if err != nil {
		return 0, err
	}
	var port int
	if _, err := fmt.Sscanf(portStr, "%d", &port); err != nil {
		return 0, fmt.Errorf("parse port %q: %w", portStr, err)
	}
	return port, nil
}
