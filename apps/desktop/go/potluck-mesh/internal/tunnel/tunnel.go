// Package tunnel manages the userspace WireGuard device (wg-go).
//
// Week 3 additions:
//   - SetMeshIP: assigns the mesh IP to the TUN interface.
//   - SetPeers: wholesale-replaces the WG peer set via UAPI replace_peers=true.
//   - AddPeer / RemovePeer / UpdatePeerEndpoint: delta peer management.
//
// Platform TUN naming:
//   - Linux:   "potluck0"   (/dev/net/tun, requires CAP_NET_ADMIN)
//   - macOS:   "utun*"      (kernel allocates the utun index; we ask for utun9)
//   - Windows: "Wintun"     (wintun.dll driver, admin required once at install time)
//
// SetMeshIP platform implementation:
//   - Linux:   shells out to `ip addr add <ip>/32 dev <iface>` (v0 acceptable;
//              netlink post-launch polish is tracked in TODO below).
//   - macOS:   shells out to `ifconfig <iface> inet <ip> <ip>`.
//   - Windows: NOT IMPLEMENTED in week 3 — IP assignment via iphlpapi/netsh is
//              non-trivial in pure Go without cgo. The TUN device comes up without
//              an address; WG session still initialises, but the mesh IP is not
//              routable on Windows until this is resolved. A log.Warn is emitted.
//              TODO(week4-windows): implement via netsh / iphlpapi or a small cgo shim.
//
// Privacy: this package handles WireGuard encrypted packets only.
// It never inspects payload bytes and never logs anything beyond
// mesh-layer metadata.
package tunnel

import (
	"fmt"
	"log/slog"
	"net"
	"runtime"
	"strings"
	"sync"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"
)

const (
	// DefaultMTU is the WireGuard tunnel MTU. Standard choice: 1420 for
	// IPv4 overhead, 1280 for IPv6-safe minimum. The design doc goes IPv6-first
	// so we pick the conservative 1280 to avoid PMTU issues.
	DefaultMTU = 1280

	// wgPort is the UDP port the WG device listens on for encrypted traffic.
	// 51820 is the WireGuard default. Using a fixed port makes firewall rules
	// predictable; random port is deferred to the STUN + hole-punch work in week 3.
	wgPort = 51820
)

// Peer describes a WireGuard peer to be configured on the tunnel.
type Peer struct {
	// PublicKey is the base64-encoded WireGuard public key.
	PublicKey string
	// AllowedIPs is the list of CIDR prefixes routed through this peer.
	// For mesh peers this is always a single /32 (the peer's mesh_ip).
	AllowedIPs []string
	// Endpoint is the "ip:port" of the peer's UDP endpoint.
	// Empty means the peer is known but not yet reachable (no STUN endpoint).
	Endpoint string
	// PersistentKeepalive is the WG keepalive interval in seconds.
	// MUST default to 0 — the design doc mandates keepalive=off by default
	// to protect battery on laptops and cellular. Only enable when the user
	// explicitly opts into "stay reachable while idle."
	// TODO(keepalive-toggle): wire a user setting that sets this to 60+.
	PersistentKeepalive int
}

// Tunnel wraps a userspace WireGuard device and its associated TUN interface.
type Tunnel struct {
	mu      sync.Mutex // protects IpcSet calls (wg-go's IpcSet is not documented as thread-safe)
	dev     *device.Device
	tdev    tun.Device
	ifName  string
	meshIP  net.IP
	wgPort  int
}

// New creates and starts the userspace WireGuard device.
//
// privateKeyBase64 is the WG private key as a base64 string (32 bytes).
// meshIPCIDR is optional on first boot (empty string is fine); call SetMeshIP
// after the welcome message delivers the assigned mesh IP.
func New(privateKeyBase64 string, meshIPCIDR string) (*Tunnel, error) {
	ifName := tunInterfaceName()

	slog.Info("tunnel: creating TUN device", "interface", ifName, "platform", runtime.GOOS)

	tdev, err := tun.CreateTUN(ifName, DefaultMTU)
	if err != nil {
		return nil, fmt.Errorf("tunnel: create TUN %q: %w", ifName, err)
	}

	// Bind uses the default connection handler, which performs IPv6-first
	// source address selection on platforms that support it (Linux, macOS).
	// On Linux with CGNAT (T-Mobile Home Internet, Starlink), IPv4 is used
	// as fallback automatically.
	bind := conn.NewDefaultBind()

	logger := newWGLogger()
	dev := device.NewDevice(tdev, bind, logger)

	// Resolve the actual interface name (macOS may allocate a different utun index).
	realName, _ := tdev.Name()
	if realName == "" {
		realName = ifName
	}

	t := &Tunnel{
		dev:    dev,
		tdev:   tdev,
		ifName: realName,
		wgPort: wgPort,
	}

	if err := t.applyBaseConfig(privateKeyBase64); err != nil {
		dev.Close()
		return nil, fmt.Errorf("tunnel: apply base config: %w", err)
	}

	if err := dev.Up(); err != nil {
		dev.Close()
		return nil, fmt.Errorf("tunnel: bring device up: %w", err)
	}

	slog.Info("tunnel: WireGuard device up", "interface", realName, "port", wgPort)

	if meshIPCIDR != "" {
		ip, _, err := net.ParseCIDR(meshIPCIDR)
		if err != nil {
			dev.Close()
			return nil, fmt.Errorf("tunnel: parse mesh IP CIDR %q: %w", meshIPCIDR, err)
		}
		if err := t.SetMeshIP(ip.String()); err != nil {
			// Non-fatal: log and continue. Mesh IP assignment failing at init
			// is recoverable (will be retried on next welcome).
			slog.Warn("tunnel: initial SetMeshIP failed", "ip", ip.String(), "err", err)
		}
	}

	return t, nil
}

// Close gracefully shuts down the WireGuard device and TUN interface.
func (t *Tunnel) Close() {
	slog.Info("tunnel: closing WireGuard device")
	t.dev.Close()
}

// SetMeshIP assigns the given IP address to the TUN interface so that traffic
// destined for this node's mesh_ip is routed into the WG tunnel.
//
// The IP is a bare address without a prefix length; the interface is configured
// with a /32 (host route). Routing the household /24 is deferred to a later week
// when we have per-platform route management sorted.
//
// Platform behaviour:
//   - Linux: shells out to `ip addr add <ip>/32 dev <iface>`
//   - macOS: shells out to `ifconfig <iface> inet <ip> <ip>` (point-to-point)
//   - Windows: logs a warning and returns without error — see TODO above.
func (t *Tunnel) SetMeshIP(meshIP string) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	ip := net.ParseIP(meshIP)
	if ip == nil {
		return fmt.Errorf("tunnel: invalid mesh IP %q", meshIP)
	}
	t.meshIP = ip

	if err := assignInterfaceAddress(t.ifName, meshIP); err != nil {
		return fmt.Errorf("tunnel: assign interface address: %w", err)
	}

	slog.Info("tunnel: mesh IP assigned to interface",
		"interface", t.ifName,
		"mesh_ip", meshIP,
	)
	return nil
}

// SetPeers atomically replaces all WG peers on the device with the provided
// list. Uses the UAPI `replace_peers=true` directive so stale peers are
// removed in the same transaction.
//
// Peers with an empty Endpoint are configured without an endpoint — they are
// known to WG (and will accept inbound traffic if they initiate) but we
// cannot initiate outbound sessions to them until their endpoint arrives via
// a roster_update.
func (t *Tunnel) SetPeers(peers []Peer) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	if len(peers) == 0 {
		// Explicit replace with no peers: remove all existing peers.
		if err := t.dev.IpcSet("replace_peers=true\n"); err != nil {
			return fmt.Errorf("tunnel: clear peers: %w", err)
		}
		slog.Info("tunnel: all peers cleared")
		return nil
	}

	var sb strings.Builder
	sb.WriteString("replace_peers=true\n")
	for _, p := range peers {
		appendPeerConfig(&sb, p)
	}

	if err := t.dev.IpcSet(sb.String()); err != nil {
		return fmt.Errorf("tunnel: set peers: %w", err)
	}

	slog.Info("tunnel: peers configured", "count", len(peers))
	return nil
}

// AddPeer adds a single peer. If the peer already exists (same public key),
// the existing configuration is replaced.
func (t *Tunnel) AddPeer(p Peer) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	var sb strings.Builder
	appendPeerConfig(&sb, p)

	if err := t.dev.IpcSet(sb.String()); err != nil {
		return fmt.Errorf("tunnel: add peer %s: %w", p.PublicKey, err)
	}

	slog.Info("tunnel: peer added",
		"pubkey_prefix", truncatePubkey(p.PublicKey),
		"endpoint", p.Endpoint,
		"allowed_ips", p.AllowedIPs,
	)
	return nil
}

// RemovePeer removes the peer identified by base64 public key.
func (t *Tunnel) RemovePeer(pubkey string) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	pubkeyHex, err := base64ToHex(pubkey)
	if err != nil {
		return fmt.Errorf("tunnel: decode pubkey: %w", err)
	}

	config := fmt.Sprintf("public_key=%s\nremove=true\n", pubkeyHex)
	if err := t.dev.IpcSet(config); err != nil {
		return fmt.Errorf("tunnel: remove peer %s: %w", pubkey, err)
	}

	slog.Info("tunnel: peer removed", "pubkey_prefix", truncatePubkey(pubkey))
	return nil
}

// UpdatePeerEndpoint changes the endpoint for an existing peer without
// disturbing its allowed_ips or keepalive setting.
func (t *Tunnel) UpdatePeerEndpoint(pubkey, endpoint string) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	pubkeyHex, err := base64ToHex(pubkey)
	if err != nil {
		return fmt.Errorf("tunnel: decode pubkey: %w", err)
	}

	if endpoint == "" {
		slog.Debug("tunnel: UpdatePeerEndpoint called with empty endpoint, no-op",
			"pubkey_prefix", truncatePubkey(pubkey),
		)
		return nil
	}

	config := fmt.Sprintf("public_key=%s\nendpoint=%s\n", pubkeyHex, endpoint)
	if err := t.dev.IpcSet(config); err != nil {
		return fmt.Errorf("tunnel: update peer endpoint %s: %w", pubkey, err)
	}

	slog.Info("tunnel: peer endpoint updated",
		"pubkey_prefix", truncatePubkey(pubkey),
		"endpoint", endpoint,
	)
	return nil
}

// appendPeerConfig writes the UAPI stanza for a single peer to sb.
// Format follows https://www.wireguard.com/xplatform/#configuration-protocol
func appendPeerConfig(sb *strings.Builder, p Peer) {
	pubkeyHex, err := base64ToHex(p.PublicKey)
	if err != nil {
		slog.Error("tunnel: cannot encode peer pubkey, skipping peer",
			"pubkey_prefix", truncatePubkey(p.PublicKey),
			"err", err,
		)
		return
	}

	sb.WriteString("public_key=")
	sb.WriteString(pubkeyHex)
	sb.WriteByte('\n')

	if p.Endpoint != "" {
		sb.WriteString("endpoint=")
		sb.WriteString(p.Endpoint)
		sb.WriteByte('\n')
	}

	for _, cidr := range p.AllowedIPs {
		sb.WriteString("allowed_ip=")
		sb.WriteString(cidr)
		sb.WriteByte('\n')
	}

	// Persistent keepalive: emit the directive only when non-zero.
	// Default 0 means "off" per the design doc — critical for battery health.
	if p.PersistentKeepalive > 0 {
		fmt.Fprintf(sb, "persistent_keepalive_interval=%d\n", p.PersistentKeepalive)
	}
}

// applyBaseConfig writes the private key and listen port via the wg-go
// UAPI-compatible configuration interface.
// Format follows https://www.wireguard.com/xplatform/#configuration-protocol
func (t *Tunnel) applyBaseConfig(privateKeyBase64 string) error {
	privKeyHex, err := base64ToHex(privateKeyBase64)
	if err != nil {
		return fmt.Errorf("decode private key: %w", err)
	}

	config := fmt.Sprintf("private_key=%s\nlisten_port=%d\n", privKeyHex, t.wgPort)
	return t.dev.IpcSet(config)
}

// tunInterfaceName returns the platform-appropriate TUN interface name.
//
//   - Linux: "potluck0" — conventional naming, udev-friendly.
//   - macOS: "utun9"  — the kernel may allocate a different index
//     (utun0..utun15 are common); we request utun9 to avoid
//     clashing with VPN software that typically grabs utun0-utun3.
//   - Windows: "Wintun" — fixed name expected by wintun.dll.
func tunInterfaceName() string {
	switch runtime.GOOS {
	case "darwin":
		return "utun9"
	case "windows":
		return "Wintun"
	default: // linux + others
		return "potluck0"
	}
}

// newWGLogger creates a wg-go device.Logger that forwards to slog.
// Errors are logged at Error level; anything else at Debug to avoid log spam
// from normal WG keepalive traffic.
// Privacy: wg-go's logger never receives payload content — only control-plane
// events (peer handshake, key rotation, tunnel state changes).
func newWGLogger() *device.Logger {
	return &device.Logger{
		Verbosef: func(format string, args ...interface{}) {
			slog.Debug(fmt.Sprintf("[wg] "+format, args...))
		},
		Errorf: func(format string, args ...interface{}) {
			slog.Error(fmt.Sprintf("[wg] "+format, args...))
		},
	}
}

// truncatePubkey returns the first 8 characters of a base64 public key for
// safe logging (enough to identify the peer, not enough to be useful for
// anything else).
func truncatePubkey(pubkey string) string {
	if len(pubkey) > 8 {
		return pubkey[:8] + "…"
	}
	return pubkey
}
