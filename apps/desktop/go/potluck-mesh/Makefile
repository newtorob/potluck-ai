# potluck-mesh build targets.
#
# Produces platform-specific binaries in dist/.
# Run `make` to see available targets.
#
# Requirements:
#   - Go 1.22+
#   - Windows binaries are built with CGO_ENABLED=0. wintun.dll is loaded at
#     run time and is staged next to the binary (see third_party/windows/README.md).

MODULE := github.com/potluck-ai/potluck-mesh
CMD    := ./cmd/potluck-mesh
DIST   := dist

# Version is the git tag or commit short hash.
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
LDFLAGS := -X main.version=$(VERSION) -s -w

.PHONY: help build build-all run clean tidy lint

# Default coordinator + DERP relay URLs for `make run`. Override with env.
COORDINATOR_URL ?= http://127.0.0.1:8400
DERP_URL        ?= tcp+insecure://127.0.0.1:8443

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build for the current platform into dist/
	@mkdir -p $(DIST)
	go build -ldflags "$(LDFLAGS)" -o $(DIST)/potluck-mesh$(shell go env GOEXE) $(CMD)
	@echo "built: $(DIST)/potluck-mesh$(shell go env GOEXE)"
	@if [ "$(shell go env GOOS)" = "windows" ]; then \
		cp third_party/windows/wintun.dll $(DIST)/wintun.dll; \
		echo "staged: $(DIST)/wintun.dll (see third_party/windows/README.md)"; \
	fi

run: ## Run the sidecar in the current shell (uses COORDINATOR_URL + DERP_URL)
	go run $(CMD) --coordinator-url $(COORDINATOR_URL) --derp-url $(DERP_URL)

build-all: ## Build for all 5 target triples into dist/
	@mkdir -p $(DIST)
	@$(MAKE) _build GOOS=darwin  GOARCH=arm64  OUT=$(DIST)/potluck-mesh-darwin-arm64
	@$(MAKE) _build GOOS=darwin  GOARCH=amd64  OUT=$(DIST)/potluck-mesh-darwin-amd64
	@$(MAKE) _build GOOS=linux   GOARCH=amd64  OUT=$(DIST)/potluck-mesh-linux-amd64
	@$(MAKE) _build GOOS=linux   GOARCH=arm64  OUT=$(DIST)/potluck-mesh-linux-arm64
	@$(MAKE) _build GOOS=windows GOARCH=amd64  OUT=$(DIST)/potluck-mesh-windows-amd64.exe CGO_ENABLED_OVERRIDE=0
	@cp third_party/windows/wintun.dll $(DIST)/wintun.dll
	@echo ""
	@echo "All builds complete:"
	@ls -lh $(DIST)/

# Internal target: invoked by build-all with GOOS/GOARCH/OUT set.
# CGO_ENABLED_OVERRIDE is used to force CGO_ENABLED=0 for Windows cross-builds
# where cgo would require a Windows toolchain.
_build:
	CGO_ENABLED=$(if $(CGO_ENABLED_OVERRIDE),$(CGO_ENABLED_OVERRIDE),1) \
	GOOS=$(GOOS) GOARCH=$(GOARCH) \
	go build -ldflags "$(LDFLAGS)" -o $(OUT) $(CMD)
	@echo "built: $(OUT)"

tidy: ## Run go mod tidy
	go mod tidy

lint: ## Run go vet
	go vet ./...

clean: ## Remove build artifacts
	rm -rf $(DIST)

.DEFAULT_GOAL := help
