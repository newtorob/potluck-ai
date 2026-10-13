package tunnel

import (
	"strings"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
)

// TestValidateEndpoint_TableAndInjection is the test for WireGuard UAPI
// config injection: an endpoint string that
// contains an embedded newline (as would arrive from a compromised/buggy
// coordinator or peer) must never reach IpcSet, because wireguard-go's UAPI
// parser treats "\n" as a directive separator - an unvalidated endpoint like
// "1.1.1.1:1\nallowed_ip=0.0.0.0/0\n" would inject an extra allowed_ip
// directive and widen that peer's allowed-ips past its intended /32.
func TestValidateEndpoint_TableAndInjection(t *testing.T) {
	tests := []struct {
		name      string
		endpoint  string
		wantValid bool
	}{
		// --- valid endpoints ---
		{
			name:      "valid ipv4 and port",
			endpoint:  "10.0.0.7:51820",
			wantValid: true,
		},
		{
			name:      "valid ipv4 low port",
			endpoint:  "192.168.1.20:1",
			wantValid: true,
		},
		{
			name:      "valid ipv4 max port",
			endpoint:  "203.0.113.5:65535",
			wantValid: true,
		},
		{
			name:      "valid bracketed ipv6 and port",
			endpoint:  "[2606:4700:4700::1111]:51820",
			wantValid: true,
		},
		{
			name:      "valid bracketed ipv6 loopback",
			endpoint:  "[::1]:51820",
			wantValid: true,
		},

		// --- the injection payload ---
		{
			name:      "newline injection widening allowed_ip",
			endpoint:  "1.1.1.1:1\nallowed_ip=0.0.0.0/0\n",
			wantValid: false,
		},
		{
			name:      "newline injection replace_allowed_ips",
			endpoint:  "10.0.0.7:51820\nreplace_allowed_ips=true\nallowed_ip=0.0.0.0/0\n",
			wantValid: false,
		},
		{
			name:      "carriage-return-newline injection",
			endpoint:  "10.0.0.7:51820\r\nallowed_ip=0.0.0.0/0\r\n",
			wantValid: false,
		},
		{
			name:      "bare newline in port field only",
			endpoint:  "10.0.0.7:518\n20",
			wantValid: false,
		},

		// --- structurally malformed ---
		{
			name:      "missing port",
			endpoint:  "10.0.0.7",
			wantValid: false,
		},
		{
			name:      "missing port with trailing colon",
			endpoint:  "10.0.0.7:",
			wantValid: false,
		},
		{
			name:      "port out of range too high",
			endpoint:  "10.0.0.7:65536",
			wantValid: false,
		},
		{
			name:      "port out of range zero",
			endpoint:  "10.0.0.7:0",
			wantValid: false,
		},
		{
			name:      "port negative",
			endpoint:  "10.0.0.7:-1",
			wantValid: false,
		},
		{
			name:      "garbage host",
			endpoint:  "not-an-ip:51820",
			wantValid: false,
		},
		{
			name:      "garbage port",
			endpoint:  "10.0.0.7:abc",
			wantValid: false,
		},
		{
			name:      "empty string",
			endpoint:  "",
			wantValid: false,
		},
		{
			name:      "host with embedded UAPI-looking key=value, no colon-port split",
			endpoint:  "public_key=deadbeef",
			wantValid: false,
		},
		{
			name:      "unbracketed ipv6 (ambiguous colon count)",
			endpoint:  "2606:4700:4700::1111:51820",
			wantValid: false,
		},

		// --- synthetic DERP endpoint carve-out (validateEndpoint must accept
		// this format, or the entire DERP fallback path breaks - see
		// validateEndpoint's doc comment) ---
		{
			name:      "valid synthetic DERP endpoint",
			endpoint:  derp.SyntheticEndpointString([32]byte{0xde, 0xad, 0xbe, 0xef}),
			wantValid: true,
		},
		{
			name:      "derp prefix but wrong length is NOT treated as synthetic (falls through to host:port, fails)",
			endpoint:  "derp:deadbeef",
			wantValid: false,
		},
		{
			name:      "derp-prefixed string with injected newline is rejected (format check is exact, not prefix-only)",
			endpoint:  "derp:" + strings.Repeat("a", 64) + "\nallowed_ip=0.0.0.0/0\n",
			wantValid: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := validateEndpoint(tc.endpoint)
			gotValid := err == nil
			if gotValid != tc.wantValid {
				t.Errorf("validateEndpoint(%q) = err %v (valid=%v), want valid=%v",
					tc.endpoint, err, gotValid, tc.wantValid)
			}
		})
	}
}

// TestAppendPeerConfig_RejectsInjectedEndpoint verifies that appendPeerConfig
// (the function that actually builds the UAPI text handed to IpcSet) rejects
// a malicious endpoint and - critically - writes nothing to the builder, so
// a caller that ignores the error can't accidentally emit a half-written or
// injected stanza.
func TestAppendPeerConfig_RejectsInjectedEndpoint(t *testing.T) {
	p := Peer{
		// Any syntactically valid base64; base64ToHex just needs to succeed
		// so we reach the endpoint validation step.
		PublicKey:  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
		AllowedIPs: []string{"100.64.0.7/32"},
		Endpoint:   "1.1.1.1:1\nallowed_ip=0.0.0.0/0\n",
	}

	var sb strings.Builder
	err := appendPeerConfig(&sb, p)
	if err == nil {
		t.Fatalf("appendPeerConfig with injected endpoint returned nil error, want rejection")
	}
	if sb.Len() != 0 {
		t.Fatalf("appendPeerConfig wrote %q to builder despite rejecting the peer; want empty output", sb.String())
	}
}

// TestAppendPeerConfig_ValidEndpointUnaffected is a control test: a normal,
// valid peer config must still produce the exact expected UAPI stanza after
// the validation change (no behavior change for the non-malicious path).
func TestAppendPeerConfig_ValidEndpointUnaffected(t *testing.T) {
	p := Peer{
		PublicKey:  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
		AllowedIPs: []string{"100.64.0.7/32"},
		Endpoint:   "10.0.0.7:51820",
	}

	var sb strings.Builder
	if err := appendPeerConfig(&sb, p); err != nil {
		t.Fatalf("appendPeerConfig with valid endpoint returned error: %v", err)
	}

	got := sb.String()
	if !strings.Contains(got, "endpoint=10.0.0.7:51820\n") {
		t.Errorf("expected endpoint directive in output, got %q", got)
	}
	if !strings.Contains(got, "allowed_ip=100.64.0.7/32\n") {
		t.Errorf("expected allowed_ip directive in output, got %q", got)
	}
	if !strings.Contains(got, "replace_allowed_ips=true\n") {
		t.Errorf("expected replace_allowed_ips directive in output, got %q", got)
	}
	// Sanity: the injected directive from the malicious test case must never
	// appear for a clean input.
	if strings.Contains(got, "0.0.0.0/0") {
		t.Errorf("unexpected wide allowed_ip leaked into clean output: %q", got)
	}
}
