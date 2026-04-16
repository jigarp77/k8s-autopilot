# Contributing to K8s Autopilot

## Development setup

```bash
git clone https://github.com/YOUR_ORG/k8s-autopilot
cd k8s-autopilot
python -m venv .venv && source .venv/bin/activate
make install
```

## Coding standards

- Python 3.11+, async everywhere
- `ruff` for linting, `black` for formatting (`make fmt`)
- `mypy` for type checking — all public functions must have type annotations
- Every new module needs unit tests in `tests/unit/`
- New remediation actions must include a docstring explaining preconditions and side effects

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add evicted pod cleanup action
fix: prevent duplicate approval messages on retry
docs: update CRD schema for minRestartCount
test: add circuit breaker half-open transition test
chore: pin anthropic to 0.28.x
```

## Adding a remediation action

1. Create or add to `autopilot/remediations/your_module.py`
2. Use `@registry.register("action_key", description="...", safe_auto=True/False)`
3. Import the module in `autopilot/main.py`
4. Add a unit test in `tests/unit/`
5. Document the action in `README.md` under "Supported triggers"

## Pull request checklist

- [ ] `make lint` passes with no errors
- [ ] `make test` passes with coverage ≥ 70%
- [ ] New behaviour is tested
- [ ] `CHANGELOG.md` updated if this is a user-visible change
- [ ] No secrets, credentials, or personal data in code or tests
