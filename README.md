# sim2real

Pipeline for transferring simulation-discovered routing algorithms from [inference-sim](inference-sim/) to production [llm-d-inference-scheduler](llm-d-inference-scheduler/) scorer plugins.

## Quick Start

```bash
git submodule update --init --recursive
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The pipeline has four entry points:

| Script | Purpose |
|--------|---------|
| `pipeline/setup.py` | One-time cluster bootstrap (namespace, RBAC, secrets, PVCs, Tekton) |
| `pipeline/prepare.py` | 6-phase state machine: context → translate → assembly → gate |
| `pipeline/deploy.py` | Build EPP image, submit Tekton packages, collect results |
| `pipeline/run.py` | List, inspect, and switch between runs |

**Environment variables** (set before running any pipeline script):

| Variable | Used by | Description |
|----------|---------|-------------|
| `HF_TOKEN` | `setup.py` | HuggingFace API token for model image pull |
| `QUAY_ROBOT_USERNAME` + `QUAY_ROBOT_TOKEN` | `setup.py` | Quay robot credentials (use this **or** GitHub) |
| `GITHUB_TOKEN` | `setup.py` | GitHub PAT (`write:packages`) for ghcr.io (use this **or** Quay) |
| `NAMESPACE` | `setup.py`, `deploy.py` | Kubernetes namespace (falls back to value saved by setup) |

All of these can also be passed as `--flags` to `setup.py` — run `python pipeline/setup.py --help` to see all options. `deploy.py` reads `NAMESPACE` from the saved setup config if the env var is not set.

**Typical flow:**

```bash
# 1. One-time cluster setup
python pipeline/setup.py

# 2. Prepare: runs phases 1–6; pauses at phase 3 for AI translation (/sim2real-translate)
python pipeline/prepare.py

# 3. Deploy to cluster and collect results
python pipeline/deploy.py
python pipeline/deploy.py collect

# 4. Manage runs
python pipeline/run.py list
python pipeline/run.py inspect <run-name>
python pipeline/run.py switch <run-name>
```

Run `python pipeline/<script>.py --help` for full options on any entry point.

## Tests

```bash
python -m pytest pipeline/ -v
```

See `CLAUDE.md` for artifact contracts and exit code semantics.

> For help when things go wrong, see [Troubleshooting](docs/contributing/troubleshooting.md).

## Prerequisites

### EPP image registry (`pipeline/deploy.py`)

**Configure your registry hub** in `config/env_defaults.yaml`:
```yaml
stack:
  gaie:
    epp_image:
      build:
        hub: ghcr.io/<your-org>   # e.g. ghcr.io/kalantar
```

**Log in to the registry** (requires a GitHub PAT with `write:packages`):
```bash
echo $GITHUB_PAT | podman login ghcr.io -u <your-github-username> --password-stdin
# or: docker login ghcr.io
```

`podman` or `docker` must be on `PATH`.
