.PHONY: build run test docker-build lint

BINARY := bin/potluck-derp
DOCKER_TAG := potluck-derp:dev

build:
	@mkdir -p bin
	go build -o $(BINARY) ./cmd/potluck-derp/

run: build
	POTLUCK_DERP_JWT_KEY=dev-only-insecure-key ./$(BINARY) --insecure-cleartext

test:
	go test ./...

lint:
	go vet ./...

docker-build:
	docker build -t $(DOCKER_TAG) .

# Cross-compile for the deploy targets.
build-linux-amd64:
	@mkdir -p bin
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags="-s -w" -o bin/potluck-derp-linux-amd64 ./cmd/potluck-derp/

build-linux-arm64:
	@mkdir -p bin
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags="-s -w" -o bin/potluck-derp-linux-arm64 ./cmd/potluck-derp/
