# potluck-derp

A TCP relay for the Potluck mesh, modeled on Tailscale's DERP (Designated Encrypted Relay for Packets).
It carries WireGuard packets between authenticated peers when a direct UDP hole-punch fails (symmetric NAT,
restrictive corporate firewalls).

## Protocol

The relay uses Potluck's own framing, which differs from Tailscale's DERP wire format. The server is in
`internal/relay/relay.go`; the client is in `apps/desktop/go/potluck-mesh/internal/derp/client.go`.

After the TCP connection (TLS in production, cleartext with `--insecure-cleartext` for local dev):
1. Client sends a JSON line: `{"type":"auth","token":"<JWT>"}`
2. Server validates the JWT: an HMAC signature made with `POTLUCK_DERP_JWT_KEY` (the coordinator issues HS256 tokens) and the claims `wg_pubkey` (the base64-encoded 32-byte WireGuard public key), `region` (must equal the relay's region), `exp` and `iat`.
3. Server replies: `{"type":"auth_ok","region":"ord","server_time":<unix>}`. On failure it sends `{"type":"auth_error","message":"..."}` and closes the connection.
4. Frame stream in both directions, with a 2-byte big-endian length that counts the payload only:
   - Client → Server: `[uint16 payload-len][32-byte dest-pubkey][payload]`
   - Server → Client: `[uint16 payload-len][32-byte src-pubkey][payload]`

Each client is limited to 1000 packets per second; packets over the limit are dropped.

Privacy: the relay sees encrypted bytes only (WireGuard end-to-end encryption sits above this layer). Payloads are never logged. Public keys are truncated to 8 characters in logs.

## Running locally (dev)

```sh
cd services/derp
make run
# Starts on :8443 (cleartext) and :8444 (health)
```

`GET :8444/health` returns the status, the number of connected clients and the total bytes forwarded.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `POTLUCK_DERP_JWT_KEY` | required | HMAC signing key (shared with coordinator) |
| `POTLUCK_DERP_PORT` | `8443` | Relay TCP port |
| `POTLUCK_DERP_HEALTH_PORT` | `8444` | Health HTTP port |
| `POTLUCK_DERP_REGION` | `ord` | Region identifier |
| `POTLUCK_DERP_MAX_CLIENTS` | `10000` | Max concurrent connections |

Without `--insecure-cleartext`, the relay requires `--tls-cert` and `--tls-key`.

## Building and testing

```sh
make build               # Current platform
make test                # go test ./...
make lint                # go vet ./...
make docker-build        # Docker image
make build-linux-amd64   # Cross-compile for Linux amd64
make build-linux-arm64   # Cross-compile for Linux arm64
```
