# Python dependency locks

`py310.txt`, `py311.txt`, and `py312.txt` are complete transitive resolutions for the supported CPython minors. `pyproject.toml` declares compatibility; these files record the exact accepted resolution.

Regenerate with the same command recorded in each file header, review the diff, then run:

```bash
for version in 3.10 3.11 3.12; do
  DATACENTER_PYTHON=".venv-${version//./}/bin/python" bash scripts/ci.sh backend
done
npm --prefix webui run test:e2e
```

The scheduled `Dependency Refresh` workflow regenerates candidates, runs all three backend versions plus service and browser acceptance, and uploads the candidate locks and structured receipts. A refresh never changes published receipts or tags automatically.
