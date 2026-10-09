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
//     netlink post-launch polish is tracked in TODO below).
//   - macOS:   shells out to `ifconfig <iface> inet <ip> <ip>`.
//   - Windows: NOT IMPLEMENTED in week 3 — IP assignment via iphlpapi/netsh is
//     non-trivial in pure Go without cgo. The TUN device comes up without
//     an address; WG session still initialises, but the mesh IP is not
//     routable on Windows until this is resolved. A log.Warn is emitted.
//     TODO(week4-windows): implement via netsh / iphlpapi or a small cgo shim.
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
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/util"
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
	// Private marks a peer that belongs to someone outside the household (a
	// circle link). Its endpoint is another person's address, so the tunnel
	// never logs it, at any level (this sidecar writes debug lines to the same
	// file), and never writes the relay endpoint, which spells out the peer's
	// whole key. A household peer's log lines are not changed.
	Private bool
}

// endpointForLog is what the tunnel may write about a peer's endpoint at info
// and error level: the endpoint for a household peer, nothing for a private one.
func endpointForLog(p Peer) string {
	if p.Private {
		return "<not logged>"
	}
	return truncateForLog(p.Endpoint)
}

// errForLog is the error a rejection line may carry: the error itself for a
// household peer, a fixed phrase for a private one, because a validation error
// quotes the endpoint it refused.
func errForLog(p Peer, err error) any {
	if p.Private {
		return "invalid config (details withheld for a circle peer)"
	}
	return err
}

// Tunnel wraps a userspace WireGuard device and its associated TUN interface.
type Tunnel struct {
	mu     sync.Mutex // protects IpcSet calls (wg-go's IpcSet is not documented as thread-safe)
	dev    *device.Device
	tdev   tun.Device
	ifName string
	meshIP net.IP
	wgPort int
	filter *PacketFilter // nil when no packet policy was installed

	// keep, when set, names WireGuard peers that live outside the household
	// roster (circle links) and that SetPeers must carry over. See SetKeptPeers.
	keep atomic.Pointer[func() []Peer]
}

// Option configures optional behaviour for New.
type Option func(*tunnelOptions)

type tunnelOptions struct {
	bind   conn.Bind
	filter *PacketFilter
}

// WithBind supplies a custom conn.Bind implementation to the WireGuard device
// instead of the default conn.NewDefaultBind(). Use this to install DerpBind
// when DERP relay fallback is enabled.
//
// Example:
//
//	b := derp.NewDerpBind(conn.NewDefaultBind(), derpClient)
//	t, err := tunnel.New(privKey, meshIP, tunnel.WithBind(b))
func WithBind(b conn.Bind) Option {
	return func(o *tunnelOptions) {
		o.bind = b
	}
}

// WithPacketPolicy installs a packet filter around the TUN device that
// wireguard-go reads and writes (see PacketFilter). Without it every packet
// wireguard-go delivers reaches the host. SetMeshIP keeps the filter told of
// this host's own address.
func WithPacketPolicy(f *PacketFilter) Option {
	return func(o *tunnelOptions) {
		o.filter = f
	}
}

// New creates and starts the userspace WireGuard device.
//
// privateKeyBase64 is the WG private key as a base64 string (32 bytes).
// meshIPCIDR is optional on first boot (empty string is fine); call SetMeshIP
// after the welcome message delivers the assigned mesh IP.
// opts are optional functional options; see WithBind.
func New(privateKeyBase64 string, meshIPCIDR string, opts ...Option) (*Tunnel, error) {
	o := &tunnelOptions{}
	for _, opt := range opts {
		opt(o)
	}

	ifName := tunInterfaceName()

	slog.Info("tunnel: creating TUN device", "interface", ifName, "platform", runtime.GOOS)

	tdev, err := tun.CreateTUN(ifName, DefaultMTU)
	if err != nil {
		return nil, fmt.Errorf("tunnel: create TUN %q: %w", ifName, err)
	}

	// Bind: use the caller-supplied bind if provided (e.g. DerpBind for relay
	// fallback), otherwise fall back to the standard OS UDP bind.
	// The default bind performs IPv6-first source address selection on
	// platforms that support it (Linux, macOS).
	var bind conn.Bind
	if o.bind != nil {
		bind = o.bind
	} else {
		bind = conn.NewDefaultBind()
	}

	logger := newWGLogger()
	// wireguard-go gets the filtered view of the TUN device when a packet policy
	// was supplied. Everything else here keeps the real device.
	var wgTUN tun.Device = tdev
	if o.filter != nil {
		wgTUN = o.filter.Wrap(tdev)
	}
	dev := device.NewDevice(wgTUN, bind, logger)

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
		filter: o.filter,
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
	if t.ifName != "" {
		// Remove the mesh inbound-allow rules we added on bring-up (Windows only;
		// no-op elsewhere). Best-effort -- they're ActiveStore rules that also
		// vanish on reboot, so a missed removal never lingers.
		_ = removeMeshInbound(t.ifName, t.wgPort)
	}
	t.dev.Close()
}

// WGPort returns the UDP port the WireGuard device listens on (51820 by
// default). Used by LAN direct-path discovery (internal/localep) to format
// this node's private-LAN endpoints as "ip:<wgPort>".
func (t *Tunnel) WGPort() int {
	return t.wgPort
}

// AddPeerRoute installs a /32 host route for the given peer mesh IP via this
// node's TUN interface. Required because the local mesh-IP /32 (installed by
// SetMeshIP) only covers our own address — without explicit host routes for
// each peer, the kernel falls back to the default gateway and drops the
// packets before they ever reach WG (and therefore before DERP can relay
// them). Idempotent.
func (t *Tunnel) AddPeerRoute(peerMeshIP string) error {
	if peerMeshIP == "" {
		return nil
	}
	if t.ifName == "" {
		return fmt.Errorf("tunnel: AddPeerRoute called before interface ready")
	}
	return addPeerHostRoute(t.ifName, peerMeshIP)
}

// RemovePeerRoute tears down the host route installed by AddPeerRoute.
// Called when a peer is removed from the household. Idempotent.
func (t *Tunnel) RemovePeerRoute(peerMeshIP string) error {
	if peerMeshIP == "" {
		return nil
	}
	if t.ifName == "" {
		return nil
	}
	return removePeerHostRoute(t.ifName, peerMeshIP)
}

// SetMeshIP assigns the given IP address to the TUN interface so that traffic
// destined for this node's mesh_ip is routed into the WG tunnel.
//
// The IP is a bare address without a prefix length; the interface is configured
// with a /32 (host route). Peer host routes are installed separately via
// AddPeerRoute when the roster is applied.
//
// Platform behaviour:
//   - Linux: shells out to `ip addr add <ip>/32 dev <iface>`
//   - macOS: shells out to `ifconfig <iface> inet <ip> <ip>` (point-to-point)
//   - Windows: logs a warning and returns without error — see TODO above.
func (t *Tunnel) SetMeshIP(meshIP string) error {
	ip := net.ParseIP(meshIP)
	if ip == nil {
		return fmt.Errorf("tunnel: invalid mesh IP %q", meshIP)
	}

	t.mu.Lock()
	t.meshIP = ip
	ifName := t.ifName
	wgPort := t.wgPort
	t.mu.Unlock()

	// The filter learns the address before the interface carries it, so the
	// first inbound packet to it is already judged against the right host.
	if t.filter != nil {
		t.filter.SetSelf(ip)
	}

	if err := assignInterfaceAddress(ifName, meshIP); err != nil {
		return fmt.Errorf("tunnel: assign interface address: %w", err)
	}

	slog.Info("tunnel: mesh IP assigned to interface",
		"interface", ifName,
		"mesh_ip", meshIP,
	)

	// Open inbound on the mesh interface so household peers can actually reach
	// this node's services. A fresh Windows Wintun adapter blocks inbound by
	// default, which otherwise leaves a Windows node silently unreachable on the
	// mesh (contributor mode / pipeline stages fail with no signal). No-op on
	// macOS/Linux. Best-effort: a firewall failure must not stop the mesh from
	// coming up -- we log it so it's diagnosable.
	//
	// allowMeshInbound is called outside t.mu because it shells out to
	// PowerShell on Windows (200–600ms), and t.mu is also held by
	// PeerReachability / IpcGet. Holding the lock for the full exec duration
	// would stall the reachability loop on the very first check after welcome.
	// ifName and wgPort are read-only after construction, so capturing them
	// above and using the local copies here is safe.
	if err := allowMeshInbound(ifName, wgPort); err != nil {
		slog.Warn("tunnel: could not open mesh inbound firewall; peers may be unable to reach this node",
			"interface", ifName,
			"wg_port", wgPort,
			"error", err,
		)
	}
	return nil
}

// truncateForLog returns s clipped to at most maxLogEndpointLen characters
// (with a "..." suffix if clipped) so that a malicious/malformed endpoint
// string (e.g. a newline-injection payload) never gets echoed into logs
// verbatim — that would just relocate the injection risk from IpcSet's UAPI
// stream to whatever consumes the structured log output.
const maxLogEndpointLen = 64

func truncateForLog(s string) string {
	s = strings.Map(func(r rune) rune {
		if r == '\n' || r == '\r' {
			return '_'
		}
		return r
	}, s)
	if len(s) > maxLogEndpointLen {
		return s[:maxLogEndpointLen] + "..."
	}
	return s
}

// validateEndpoint rejects any endpoint string that does not round-trip to a
// clean "host:port" pair with a syntactically valid IP address and a port in
// [1, 65535] — OR the synthetic "derp:<64-hex>" format written by
// derp.SyntheticEndpointString for DERP-routed peers (see below).
//
// This is defense-in-depth against UAPI config injection: endpoint strings
// originate from the coordinator/peer roster (untrusted wire data) and are
// interpolated directly into newline-delimited UAPI directives sent to
// wireguard-go via IpcSet. A value containing an embedded "\n" (e.g.
// "1.1.1.1:1\nallowed_ip=0.0.0.0/0\n") would inject an extra UAPI directive —
// e.g. widening a peer's allowed_ips past its intended /32 — because IpcSet
// has no concept of a single "field"; it just parses whatever newline-
// separated key=value lines it's given. net.SplitHostPort + net.ParseIP
// reject embedded newlines/extra content because the host component would
// then fail to parse as a valid IP.
//
// The coordinator is being hardened server-side in parallel, but this client
// must never trust the wire regardless of what the server does.
//
// The synthetic-endpoint carve-out is safe under the same threat model: it is
// never peer/coordinator-supplied — main.go's flipToDERP constructs it
// locally from a WG pubkey it already trusts (the roster's wg_public_key,
// decoded to exactly 32 bytes and hex-encoded) — and
// derp.IsSyntheticEndpointFormat requires an EXACT "derp:" + 64-hex-char
// shape, which has no room for an embedded newline or any other injection
// payload. Without this carve-out, every UpdatePeerEndpoint call that flips a
// peer to DERP routing (the always-on relay floor, not just the opt-in
// LAN-direct path) was rejected outright — "invalid IP host \"derp\"" — which
// left EVERY peer with no endpoint configured on the WG device at all
// (neither the synthetic DERP endpoint nor a raw direct one, since the
// roster-update path also skips raw-endpoint writes for anything the mode
// tracker / dBind consider DERP-managed). Regression introduced by commit
// f5fd5ef ("fix(mesh): validate peer endpoints before WireGuard UAPI write"),
// which predates this carve-out.
func validateEndpoint(endpoint string) error {
	if derp.IsSyntheticEndpointFormat(endpoint) {
		return nil
	}
	host, portStr, err := net.SplitHostPort(endpoint)
	if err != nil {
		return fmt.Errorf("split host:port: %w", err)
	}
	if net.ParseIP(host) == nil {
		return fmt.Errorf("invalid IP host %q", host)
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		return fmt.Errorf("invalid port %q: %w", portStr, err)
	}
	if port < 1 || port > 65535 {
		return fmt.Errorf("port %d out of range [1, 65535]", port)
	}
	return nil
}

// SetKeptPeers installs the source of peers that SetPeers must keep. SetPeers
// writes replace_peers=true, which removes every WireGuard peer that is not in
// its list, and the household roster does not contain the restricted peers of a
// circle link. Without this a welcome, which arrives on every signal reconnect
// and every coordinator restart, would silently remove them while the sidecar
// still believed they were live.
//
// fn runs inside SetPeers with the tunnel lock held, so it must not call back
// into the Tunnel and must not take a lock that a caller of the Tunnel holds
// across a Tunnel call. The circle controller answers from an immutable snapshot.
// It must return only peers that are still wanted: whatever it returns is
// configured, and a peer it leaves out is removed.
func (t *Tunnel) SetKeptPeers(fn func() []Peer) {
	if fn == nil {
		t.keep.Store(nil)
		return
	}
	t.keep.Store(&fn)
}

// keptPeers returns the kept peers that do not clash with the batch. A kept peer
// that shares a key or an allowed-ip with a batch peer is dropped: the batch is
// the household roster and it wins, because wireguard-go would otherwise let the
// later one take the other's key or address.
func (t *Tunnel) keptPeers(batch []Peer) []Peer {
	fn := t.keep.Load()
	if fn == nil {
		return nil
	}
	taken := make(map[string]bool, 2*len(batch))
	for _, p := range batch {
		taken["key:"+p.PublicKey] = true
		for _, cidr := range p.AllowedIPs {
			taken["ip:"+cidr] = true
		}
	}
	var out []Peer
	for _, p := range (*fn)() {
		clash := taken["key:"+p.PublicKey]
		for _, cidr := range p.AllowedIPs {
			clash = clash || taken["ip:"+cidr]
		}
		if clash {
			slog.Warn("tunnel: kept peer clashes with a roster peer, not carrying it over",
				"pubkey_prefix", util.TruncatePubkey(p.PublicKey),
				"allowed_ips", p.AllowedIPs,
			)
			continue
		}
		out = append(out, p)
	}
	return out
}

// SetPeers atomically replaces all WG peers on the device with the provided
// list plus the peers named by SetKeptPeers. Uses the UAPI `replace_peers=true`
// directive so stale peers are removed in the same transaction.
//
// Peers with an empty Endpoint are configured without an endpoint — they are
// known to WG (and will accept inbound traffic if they initiate) but we
// cannot initiate outbound sessions to them until their endpoint arrives via
// a roster_update.
func (t *Tunnel) SetPeers(peers []Peer) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	kept := t.keptPeers(peers)
	if len(peers) == 0 && len(kept) == 0 {
		// Explicit replace with no peers: remove all existing peers.
		if err := t.dev.IpcSet("replace_peers=true\n"); err != nil {
			return fmt.Errorf("tunnel: clear peers: %w", err)
		}
		slog.Info("tunnel: all peers cleared")
		return nil
	}

	var sb strings.Builder
	sb.WriteString("replace_peers=true\n")
	skipped := 0
	for _, p := range append(append([]Peer(nil), peers...), kept...) {
		if err := appendPeerConfig(&sb, p); err != nil {
			// A single malformed/malicious peer entry must not abort the
			// whole batch (which would tear down every other peer's config
			// via replace_peers=true) or block the rest of the roster from
			// being applied. Drop just this peer and keep going.
			slog.Error("tunnel: rejecting peer with invalid config, excluding from SetPeers batch",
				"pubkey_prefix", util.TruncatePubkey(p.PublicKey),
				"endpoint", endpointForLog(p),
				"err", errForLog(p, err),
			)
			skipped++
			continue
		}
	}

	if err := t.dev.IpcSet(sb.String()); err != nil {
		return fmt.Errorf("tunnel: set peers: %w", err)
	}

	slog.Info("tunnel: peers configured", "count", len(peers)+len(kept)-skipped, "rejected", skipped, "kept", len(kept))
	return nil
}

// AddPeer adds a single peer. If the peer already exists (same public key),
// the existing configuration is replaced.
func (t *Tunnel) AddPeer(p Peer) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	var sb strings.Builder
	if err := appendPeerConfig(&sb, p); err != nil {
		slog.Error("tunnel: rejecting AddPeer with invalid config",
			"pubkey_prefix", util.TruncatePubkey(p.PublicKey),
			"endpoint", endpointForLog(p),
			"err", errForLog(p, err),
		)
		return fmt.Errorf("tunnel: add peer %s: %w", p.PublicKey, err)
	}

	if err := t.dev.IpcSet(sb.String()); err != nil {
		return fmt.Errorf("tunnel: add peer %s: %w", p.PublicKey, err)
	}

	if p.Private {
		slog.Info("tunnel: peer added",
			"pubkey_prefix", util.TruncatePubkey(p.PublicKey),
			"allowed_ips", p.AllowedIPs,
		)
		return nil
	}
	slog.Info("tunnel: peer added",
		"pubkey_prefix", util.TruncatePubkey(p.PublicKey),
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

	slog.Info("tunnel: peer removed", "pubkey_prefix", util.TruncatePubkey(pubkey))
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
			"pubkey_prefix", util.TruncatePubkey(pubkey),
		)
		return nil
	}

	// Reject any endpoint that isn't a clean "host:port" before it ever
	// reaches IpcSet. See validateEndpoint for the UAPI config-injection
	// rationale. A rejected endpoint is a no-op for this peer, not a crash —
	// the roster-update loop that calls this per-peer must keep going for
	// the rest of the roster.
	if err := validateEndpoint(endpoint); err != nil {
		slog.Error("tunnel: rejecting UpdatePeerEndpoint with invalid endpoint",
			"pubkey_prefix", util.TruncatePubkey(pubkey),
			"endpoint", truncateForLog(endpoint),
			"err", err,
		)
		return fmt.Errorf("tunnel: update peer endpoint %s: invalid endpoint: %w", pubkey, err)
	}

	config := fmt.Sprintf("public_key=%s\nendpoint=%s\n", pubkeyHex, endpoint)
	if err := t.dev.IpcSet(config); err != nil {
		return fmt.Errorf("tunnel: update peer endpoint %s: %w", pubkey, err)
	}

	slog.Info("tunnel: peer endpoint updated",
		"pubkey_prefix", util.TruncatePubkey(pubkey),
		"endpoint", endpoint,
	)
	return nil
}

// PeerReachability queries the wg-go UAPI for the named peer's last-handshake
// time and traffic counters. The pubkey is the base64-encoded WireGuard public
// key for the peer.
//
// Returns the zero time and zero counters if the peer is not found or has never
// completed a handshake. Callers can determine reachability with:
//
//	reachable := !lastHandshake.IsZero() && time.Since(lastHandshake) < 3*time.Minute
//
// Privacy: this method parses the raw UAPI GET output but never logs it — the
// output includes the device's own private key in hex form.
func (t *Tunnel) PeerReachability(pubkeyBase64 string) (lastHandshake time.Time, rxBytes uint64, txBytes uint64, err error) {
	pubkeyHex, err := base64ToHex(pubkeyBase64)
	if err != nil {
		return time.Time{}, 0, 0, fmt.Errorf("tunnel: decode pubkey for reachability: %w", err)
	}

	// IpcGet returns the full UAPI GET response as a string. We hold the
	// mutex because IpcGet + IpcSet must not race. The installed wg-go
	// version signature is `IpcGet() (string, error)` — older versions of
	// the API took an io.Writer; if the build breaks on a future wg-go
	// bump, swap to whichever shape the new version exposes.
	t.mu.Lock()
	uapiText, ipcErr := t.dev.IpcGet()
	t.mu.Unlock()

	if ipcErr != nil {
		return time.Time{}, 0, 0, fmt.Errorf("tunnel: IpcGet: %w", ipcErr)
	}

	// Parse the UAPI output. The format is newline-separated key=value pairs.
	// Device-level fields come first, then each peer starts with a
	// `public_key=<hex>` line. We scan for the target peer's section.
	//
	// Example peer section (relevant fields only):
	//   public_key=<hex>
	//   last_handshake_time_sec=1715000000
	//   last_handshake_time_nsec=0
	//   rx_bytes=12345
	//   tx_bytes=67890
	//
	// Privacy: we parse but do not log or store the full buffer.
	inPeer := false
	var lhSec, lhNsec int64
	var rx, tx uint64

	for _, line := range strings.Split(uapiText, "\n") {
		line = strings.TrimRight(line, "\r")
		if line == "" {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}

		switch k {
		case "public_key":
			inPeer = strings.EqualFold(v, pubkeyHex)
		case "last_handshake_time_sec":
			if inPeer {
				lhSec, _ = strconv.ParseInt(v, 10, 64)
			}
		case "last_handshake_time_nsec":
			if inPeer {
				lhNsec, _ = strconv.ParseInt(v, 10, 64)
			}
		case "rx_bytes":
			if inPeer {
				rx, _ = strconv.ParseUint(v, 10, 64)
			}
		case "tx_bytes":
			if inPeer {
				tx, _ = strconv.ParseUint(v, 10, 64)
			}
		}
	}

	if lhSec > 0 {
		lastHandshake = time.Unix(lhSec, lhNsec)
	}
	return lastHandshake, rx, tx, nil
}

// PeerEndpoint returns the LIVE endpoint wireguard-go currently has configured
// for the named peer, straight from the UAPI — i.e. what Send() will actually
// dispatch to next, not what we last told it to use via UpdatePeerEndpoint/
// AddPeer. Returns "" if the peer has no endpoint configured (e.g. never
// received a packet yet) or is not found.
//
// Caveat for DERP-routed peers: the UAPI serializes the endpoint via
// conn.Endpoint.DstToString(), which for our synthetic DERP endpoint
// (internal/derp/bind.go's syntheticEndpoint) returns the ABBREVIATED
// "derp:<8-char-hex-prefix>" logging form, not the full round-trippable
// derp.SyntheticEndpointString() value. Callers that need to detect "is this
// peer still DERP-routed" must check the "derp:" prefix, not compare for
// equality against SyntheticEndpointString.
//
// Why this exists: wireguard-go performs UNCONDITIONAL endpoint roaming on
// inbound packets (SetEndpointFromPacket) — it silently repoints a peer's
// active endpoint to wherever an authenticated inbound packet from that peer
// last arrived FROM, with no way to opt a specific peer out. For a
// DERP-managed peer (flipToDERP has pinned the synthetic "derp:<pubkey>"
// endpoint), a single stray raw-UDP packet from the peer — e.g. a leftover
// hole-punch retry sent before both sides had converged on DERP, or an
// occasional packet that slips through an otherwise-failing NAT hairpin —
// is enough to silently roam the peer OFF the synthetic endpoint and onto a
// real ip:port that may not reliably route (see internal/derp/bind.go's
// DerpBind.Send doc: a non-synthetic endpoint falls straight through to raw
// UDP). The peer then looks "reachable" (recent handshake, control traffic
// flows) while application data silently black-holes — the wireguard-go
// analogue of the same-key-rejoin bug (handshake succeeds, data doesn't).
// Callers use this to detect that drift and re-assert flipToDERP.
func (t *Tunnel) PeerEndpoint(pubkeyBase64 string) (string, error) {
	pubkeyHex, err := base64ToHex(pubkeyBase64)
	if err != nil {
		return "", fmt.Errorf("tunnel: decode pubkey for endpoint check: %w", err)
	}

	t.mu.Lock()
	uapiText, ipcErr := t.dev.IpcGet()
	t.mu.Unlock()

	if ipcErr != nil {
		return "", fmt.Errorf("tunnel: IpcGet: %w", ipcErr)
	}

	inPeer := false
	endpoint := ""
	for _, line := range strings.Split(uapiText, "\n") {
		line = strings.TrimRight(line, "\r")
		if line == "" {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch k {
		case "public_key":
			inPeer = strings.EqualFold(v, pubkeyHex)
		case "endpoint":
			if inPeer {
				endpoint = v
			}
		}
	}
	return endpoint, nil
}

// IsPeerConfigured reports whether the WG device currently has a peer with the
// given public key configured. It lets callers distinguish "this peer's key
// isn't provisioned on the tunnel" (a stale/drifted key after a re-pair — fixed
// by a re-sync) from "the key is configured but no tunnel formed" (a firewall /
// NAT / VPN block) — two unreachable cases with two different fixes.
//
// Privacy: like PeerReachability, this parses the UAPI but never logs it (the
// output contains the device's private key in hex).
func (t *Tunnel) IsPeerConfigured(pubkeyBase64 string) (bool, error) {
	pubkeyHex, err := base64ToHex(pubkeyBase64)
	if err != nil {
		return false, fmt.Errorf("tunnel: decode pubkey for config check: %w", err)
	}

	t.mu.Lock()
	uapiText, ipcErr := t.dev.IpcGet()
	t.mu.Unlock()
	if ipcErr != nil {
		return false, fmt.Errorf("tunnel: IpcGet: %w", ipcErr)
	}

	for _, line := range strings.Split(uapiText, "\n") {
		line = strings.TrimRight(line, "\r")
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		if k == "public_key" && strings.EqualFold(v, pubkeyHex) {
			return true, nil
		}
	}
	return false, nil
}

// appendPeerConfig writes the UAPI stanza for a single peer to sb.
// Format follows https://www.wireguard.com/xplatform/#configuration-protocol
//
// Returns an error (and writes nothing to sb) if the peer's fields cannot be
// safely encoded — in particular, a non-empty Endpoint that fails
// validateEndpoint is a hard reject: endpoint strings are untrusted
// coordinator/peer-supplied data, and writing one straight into the
// newline-delimited UAPI stream would let an embedded "\n" inject arbitrary
// extra directives (e.g. widening allowed_ip past the peer's /32). Callers
// must not fall back to writing a partial/unvalidated stanza on error.
func appendPeerConfig(sb *strings.Builder, p Peer) error {
	pubkeyHex, err := base64ToHex(p.PublicKey)
	if err != nil {
		slog.Error("tunnel: cannot encode peer pubkey, skipping peer",
			"pubkey_prefix", util.TruncatePubkey(p.PublicKey),
			"err", err,
		)
		return fmt.Errorf("encode peer pubkey: %w", err)
	}

	if p.Endpoint != "" {
		if err := validateEndpoint(p.Endpoint); err != nil {
			return fmt.Errorf("invalid endpoint for peer %s: %w", util.TruncatePubkey(p.PublicKey), err)
		}
	}

	sb.WriteString("public_key=")
	sb.WriteString(pubkeyHex)
	sb.WriteByte('\n')

	if p.Endpoint != "" {
		sb.WriteString("endpoint=")
		sb.WriteString(p.Endpoint)
		sb.WriteByte('\n')
	}

	// When the peer carries allowed-ips, clear any existing set before listing
	// them so re-adding an already-configured peer REPLACES its allowed-ips
	// rather than merging. This is the canonical wireguard UAPI idempotent-
	// reconfig idiom: without it, a same-key rejoin (e.g. a peer that rebooted
	// and re-paired) leaves stale allowed-ip entries behind and the data path
	// never cleanly re-establishes. Harmless under replace_peers (SetPeers);
	// required for the single-peer AddPeer re-add path.
	if len(p.AllowedIPs) > 0 {
		sb.WriteString("replace_allowed_ips=true\n")
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

	return nil
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
