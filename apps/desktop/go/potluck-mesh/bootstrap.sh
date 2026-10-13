#!/usr/bin/env bash
# bootstrap.sh - run once after cloning to fetch Go dependencies.
# Requires: Go 1.22+
set -euo pipefail

cd "$(dirname "$0")"

echo "==> go mod tidy (fetches dependencies, generates go.sum)"
go mod tidy

echo "==> go vet"
go vet ./...

echo "==> go build (current platform)"
mkdir -p dist
go build -o dist/potluck-mesh$(go env GOEXE) ./cmd/potluck-mesh

echo ""
echo "Bootstrap complete. Binary at: dist/potluck-mesh$(go env GOEXE)"
echo ""
echo "Next: enroll this node via the coordinator, then run:"
echo "  ./dist/potluck-mesh --coordinator-url http://127.0.0.1:8400"
