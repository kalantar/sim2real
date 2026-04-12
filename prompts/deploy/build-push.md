---
stage: 4.75
version: "1.0"
pipeline_commit: "set-at-runtime"
description: "Stage 4.75 — Build and push treatment EPP image to developer registry"
---

# Stage 4.75: Build & Push EPP Image

Build the `llm-d-inference-scheduler` container image from the local submodule
(which contains the generated scorer plugin) and push it to the developer's
container registry. This image is used by the treatment phase in Stage 5.

The baseline and noise phases use the upstream `ghcr.io/llm-d` image. The
treatment phase requires a custom image because `blis-weighted-scoring-scorer`
is statically compiled into the binary and the upstream image does not contain it.

## Prerequisites

Verify Stage 4 passed, the equivalence gate (Stage 4.5) passed, and the registry is configured. **HALT if any check fails.**

```bash
# Stage 3 output still valid
test -f workspace/stage3_output.json || { echo "HALT: missing stage3_output.json"; exit 1; }

# Scorer is still present and builds
SCORER_FILE=$(.venv/bin/python -c "import json; print(json.load(open('workspace/stage3_output.json'))['scorer_file'])")
if [ $? -ne 0 ] || [ -z "$SCORER_FILE" ]; then
  echo "HALT: failed to extract scorer_file from workspace/stage3_output.json"; exit 1
fi
test -f "$SCORER_FILE" || { echo "HALT: scorer file missing: $SCORER_FILE"; exit 1; }
(cd llm-d-inference-scheduler && GOWORK=off go build ./...) \
  || { echo "HALT: scorer does not build"; exit 1; }

# Equivalence gate passed (Stage 4.5)
test -f workspace/equivalence_results.json || { echo "HALT: Stage 4.5 equivalence gate not run — run prompts/equivalence-gate.md first"; exit 1; }
.venv/bin/python -c "
import json, sys
d = json.load(open('workspace/equivalence_results.json'))
if not d.get('suite_a', {}).get('passed'):
    print('HALT: Suite A did not pass — scorer formula does not match simulation reference')
    sys.exit(1)
if not d.get('suite_c', {}).get('passed'):
    print('HALT: Suite C did not pass — scorer is not concurrency-safe')
    sys.exit(1)
print('Equivalence gate: Suite A and C passed')
" || exit 1

# Registry is configured (not the placeholder value)
.venv/bin/python -c "
import yaml, sys
cfg = yaml.safe_load(open('config/env_defaults.yaml'))
hub = cfg.get('stack',{}).get('gaie',{}).get('epp_image',{}).get('build',{}).get('hub','')
if not hub or 'REPLACE_ME' in hub:
    print('HALT: epp_image.build.hub is not set in config/env_defaults.yaml')
    sys.exit(1)
print(f'Registry: {hub}')
" || exit 1

# Container runtime available
command -v podman || command -v docker || { echo "HALT: neither podman nor docker found on PATH"; exit 1; }
```

## Step 1: Configure Registry (one-time)

If `epp_image.build.hub` in `config/env_defaults.yaml` still reads `ghcr.io/REPLACE_ME`,
edit it to point to your own registry:

```yaml
# config/env_defaults.yaml
stack:
  gaie:
    epp_image:
      build:
        hub: ghcr.io/<your-org>   # e.g. ghcr.io/kalantar
```

This setting persists across pipeline runs — you only need to do this once.

Ensure you are logged in to the registry:

```bash
podman login ghcr.io   # or: docker login ghcr.io
```

## Step 2: Build and Push

```bash
.venv/bin/python tools/transfer_cli.py build-push-epp \
  --scheduler-dir llm-d-inference-scheduler \
  --env config/env_defaults.yaml \
  --values workspace/tekton/algorithm_values.yaml \
  --merged-values workspace/tekton/values.yaml
```

The command:
1. Reads `epp_image.build.{hub, name, platform}` from `config/env_defaults.yaml`
2. Generates a tag `sim2real-<git-sha>` from the `llm-d-inference-scheduler` HEAD commit
3. Builds the image (cross-compiles to `linux/amd64` by default — correct for target clusters even on arm64 Mac)
4. Pushes to `<hub>/llm-d-inference-scheduler:<tag>`
5. Injects the image reference into `workspace/tekton/algorithm_values.yaml` under `stack.gaie.treatment.helmValues.inferenceExtension.image`
6. Re-runs `merge-values` to regenerate `workspace/tekton/values.yaml`
7. Re-compiles all three phase pipeline YAMLs

**Optional dry-run** (builds locally, skips push and config update — useful to verify the build before pushing):

```bash
.venv/bin/python tools/transfer_cli.py build-push-epp \
  --scheduler-dir llm-d-inference-scheduler \
  --env config/env_defaults.yaml \
  --values workspace/tekton/algorithm_values.yaml \
  --merged-values workspace/tekton/values.yaml \
  --dry-run
```

## Between-stage Validation

```bash
# Verify image reference was injected into algorithm_values.yaml
.venv/bin/python -c "
import yaml, sys
d = yaml.safe_load(open('workspace/tekton/algorithm_values.yaml'))
img = (d.get('stack',{}).get('gaie',{}).get('treatment',{})
        .get('helmValues',{}).get('inferenceExtension',{}).get('image',{}))
if not img.get('hub') or not img.get('tag'):
    print('HALT: treatment inferenceExtension.image not set in algorithm_values.yaml')
    sys.exit(1)
print(f\"Treatment EPP image: {img['hub']}/{img['name']}:{img['tag']}\")
" || exit 1

# Verify merged values.yaml was updated
test -f workspace/tekton/values.yaml || { echo "HALT: workspace/tekton/values.yaml missing"; exit 1; }

# Verify compiled treatment pipeline references the image
grep -q "inferenceExtension" workspace/tekton/compiled/treatment-pipeline.yaml || \
  { echo "HALT: treatment-pipeline.yaml missing inferenceExtension config"; exit 1; }
```

**HALT if any validation fails.** Do not proceed to Stage 5.
