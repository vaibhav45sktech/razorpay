# CI

The GitHub Actions workflow lives at `.github/workflows/ci.yml` and runs on
every push and pull request: secrets-hygiene gate first (blocks everything
else), then ruff, then the full pytest suite and the benchmark.

This folder is kept for any CI helper scripts that do not belong in the
workflow file itself.
