.PHONY: help install test lint fmt build push deploy crds clean

REGISTRY     ?= ghcr.io
IMAGE_NAME   ?= $(shell basename $(CURDIR))
IMAGE_TAG    ?= latest
NAMESPACE    ?= k8s-autopilot
KUBECONTEXT  ?= $(shell kubectl config current-context)

help:
	@echo "K8s Autopilot — developer targets"
	@echo ""
	@echo "  make install     Install all Python dependencies (dev)"
	@echo "  make test        Run unit tests with coverage"
	@echo "  make lint        Run ruff + mypy"
	@echo "  make fmt         Auto-format with black"
	@echo "  make build       Build Docker image"
	@echo "  make push        Push Docker image to registry"
	@echo "  make crds        Apply CRDs to cluster"
	@echo "  make deploy      Deploy operator to cluster"
	@echo "  make logs        Tail operator logs"
	@echo "  make dry-run     Run operator locally in dry-run mode"
	@echo "  make clean       Remove build artefacts"

install:
	pip install -r requirements-dev.txt
	pre-commit install

test:
	pytest tests/unit/ \
		--cov=operator \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		-v --tb=short
	@echo "Coverage report: htmlcov/index.html"

lint:
	ruff check operator/ tests/
	mypy operator/ --ignore-missing-imports

fmt:
	black operator/ tests/
	ruff check --fix operator/ tests/

build:
	docker build \
		--tag $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) \
		--tag $(REGISTRY)/$(IMAGE_NAME):$(shell git rev-parse --short HEAD) \
		.

push: build
	docker push $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
	docker push $(REGISTRY)/$(IMAGE_NAME):$(shell git rev-parse --short HEAD)

crds:
	kubectl apply -f crds/ --context $(KUBECONTEXT)
	kubectl wait --for=condition=Established crd/autopilotpolicies.autopilot.k8s.io --timeout=30s
	kubectl wait --for=condition=Established crd/remediationaudits.autopilot.k8s.io  --timeout=30s
	@echo "CRDs installed"

deploy: crds
	kubectl apply -f deploy/deployment.yaml --context $(KUBECONTEXT)
	kubectl rollout status deployment/k8s-autopilot -n $(NAMESPACE) --timeout=120s

logs:
	kubectl logs -f deployment/k8s-autopilot -n $(NAMESPACE) --context $(KUBECONTEXT)

dry-run:
	AUTOPILOT_MODE=dry-run \
	LOG_LEVEL=DEBUG \
	ANTHROPIC_API_KEY=$(ANTHROPIC_API_KEY) \
	python -m operator.main

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf htmlcov .coverage coverage.xml .mypy_cache .ruff_cache dist build
