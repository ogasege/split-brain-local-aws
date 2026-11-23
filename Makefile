# Split-brain local AWS: MiniStack + AWS Emulator as one local AWS.
#   make up deploy verify test down
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
TF := terraform -chdir=terraform
ENV := source scripts/env.sh

.PHONY: help routes up deploy verify test unit integration down reset destroy clean

help: ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

routes: ## render routing.toml into env, aws-config and terraform provider
	@python3 tools/render_routes.py

up: ## start AWS Emulator (:9297) and MiniStack (:4566), wait for health
	@scripts/up.sh

deploy: routes ## terraform apply across both emulators
	@$(ENV) && $(TF) init -input=false -no-color >/dev/null && $(TF) apply -input=false -auto-approve -no-color

verify: ## end-to-end check: CSV upload -> Lambda -> DynamoDB + alerts
	@scripts/verify.sh

test: unit integration ## unit tests + integration tests against the running stack

unit: ## unit tests for the Lambda logic (no emulators needed)
	@python3 -m pytest -q tests/unit

integration: ## pytest suite against the deployed split stack
	@$(ENV) && SPLIT_BRAIN_REQUIRE_STACK=1 python3 -m pytest -q tests/integration

destroy: ## terraform destroy across both emulators
	@$(ENV) && $(TF) destroy -input=false -auto-approve -no-color

down: ## stop both emulators (state is in memory and is lost)
	@scripts/down.sh
	@rm -f terraform/terraform.tfstate terraform/terraform.tfstate.backup

reset: ## wipe both emulators between test runs (then: make deploy)
	@scripts/reset.sh

clean: down ## remove generated files and local state
	@rm -rf .local terraform/.terraform terraform/provider.gen.tf .pytest_cache
