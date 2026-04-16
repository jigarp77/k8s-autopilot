.PHONY: help install test lint fmt build push deploy crds clean

REGISTRY     ?= ghcr.io
IMAGE_NAME   ?= k8s-autopilot
IMAGE_TAG    ?= latest
NAMESPACE    ?= k8s-autopilot
KUBECONTEXT  ?= $(shell kubectl config current-context 2>/dev/null)

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
	pre-commit install || true

test:
	pytest tests/unit/ \
		--cov=autopilot \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		-v --tb=short

lint:
	ruff check autopilot/ tests/
	ruff format --check autopilot/ tests/
	mypy autopilot/ --ignore-missing-imports --no-strict-optional

fmt:
	ruff format autopilot/ tests/
	ruff check --fix autopilot/ tests/

build:
	docker build \
		--tag $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) \
		.

push: build
	docker push $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

crds:
	kubectl apply -f crds/

deploy: crds
	kubectl apply -f deploy/deployment.yaml
	kubectl rollout status deployment/k8s-autopilot -n $(NAMESPACE) --timeout=120s

logs:
	kubectl logs -f deployment/k8s-autopilot -n $(NAMESPACE)

dry-run:
	AUTOPILOT_MODE=dry-run \
	LOG_LEVEL=DEBUG \
	python -m autopilot.main

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf htmlcov .coverage coverage.xml .mypy_cache .ruff_cache dist build
