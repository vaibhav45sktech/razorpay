# CI

`github-actions-ci.yml` is the GitHub Actions workflow for this project. It
lives here rather than at `.github/workflows/` for one boring reason: pushing a
file under `.github/workflows/` requires a Personal Access Token with the
**`workflow`** scope, and the token used for this repo does not have it. GitHub
rejects the whole push, not just that file.

**To activate CI** (30 seconds, once):

1. Add the `workflow` scope to your PAT — GitHub → Settings → Developer
   settings → Personal access tokens → edit the token → tick **workflow**.
2. Then:

   ```bash
   mkdir -p .github/workflows
   git mv ci/github-actions-ci.yml .github/workflows/ci.yml
   git commit -m "ci: activate GitHub Actions"
   git push
   ```

Nothing else changes — the workflow file is complete and unmodified.

## What it does

Four jobs. **Secret hygiene runs first and blocks the rest**, because it is the
automated version of a check the playbook currently asks a human to remember:

| Job | Gate |
|---|---|
| `secrets` | No key-shaped `rzp_live_…` in any tracked file; `.env` absent from the index **and** from history; the `rzp_test_` startup guard still in `config.py`; gitleaks (advisory) |
| `test` | `pytest` on Python 3.10 and 3.13 |
| `benchmark` | The scripted benchmark. **Exits non-zero if any PRD §6.1 target slips**, and uploads the metrics as an artifact |
| `lint` | `ruff` (blocking), `mypy` and `pip-audit` (advisory — a new CVE in a pin should inform, not block a demo) |

You can run every one of these locally right now:

```bash
python -m pytest -q
python -m benchmark.run_benchmark
ruff check backend benchmark scripts
```
