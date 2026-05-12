# Multi-stage build. Final image is distroless/static — no shell, no libc,
# minimal attack surface. Binary is statically linked.
#
# Target platforms: linux/amd64, linux/arm64.
# Build:  docker buildx build --platform linux/amd64,linux/arm64 -t potluck-derp .
# Run:    docker run -e POTLUCK_DERP_JWT_KEY=... potluck-derp

# ---- stage 1: build ---- #
FROM golang:1.22-alpine AS builder

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO_ENABLED=0 produces a fully static binary (no libc dependency).
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w" \
    -o /bin/potluck-derp \
    ./cmd/potluck-derp/

# ---- stage 2: final ---- #
FROM gcr.io/distroless/static:nonroot

COPY --from=builder /bin/potluck-derp /potluck-derp

# Relay port (TLS)
EXPOSE 8443
# Health port (plain HTTP)
EXPOSE 8444

ENTRYPOINT ["/potluck-derp"]
