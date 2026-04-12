# Skill Feedback Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address user feedback from testing the sim2real skills — fix setup script issues, integrate existing review-plan infrastructure, fix AI review parsing, persist setup outputs, and improve EPP image naming.

**Architecture:** Modifications to 4 existing files under `.claude/skills/` plus updates to 2 SKILL.md files. No new files needed. The AI review switches from custom scripts to the existing `/review-plan` skill from sdlc-plugins.

**Tech Stack:** Bash, Python, LiteLLM via sdlc-plugins review.sh

---

## File Structure

```
.claude/skills/
├── sim2real-setup/
│   ├── SKILL.md                          # Modify: run inside Claude, record outputs
│   └── scripts/
│       └── setup.sh                      # Modify: PVC, storageClass, Quay, OpenShift, pipeline apply, output file
├── sim2real-prepare/
│   ├── SKILL.md                          # Modify: use /review-plan instead of custom scripts
│   └── scripts/
│       ├── review_translation.py         # Modify: fix Opus/Gemini JSON parsing
│       └── build_review_request.py       # No changes
└── sim2real-deploy/
    └── SKILL.md                          # Modify: read setup outputs, EPP naming, namespace
```

---

### Task 1: Fix setup.sh — PVC storageClassName and bound check

**Files:**
- Modify: `.claude/skills/sim2real-setup/scripts/setup.sh:117-155`

- [ ] **Step 1: Fix storageClassName to `ibm-spectrum-scale-fileset`**

In `setup.sh`, replace the OpenShift storage class detection block (lines 120-128):

```bash
# Old:
STORAGE_CLASS=""
if [ "$IS_OPENSHIFT" = true ]; then
  STORAGE_CLASS="ocs-storagecluster-cephfs"
  info "OpenShift: using storageClassName=${STORAGE_CLASS}"
fi

# New:
STORAGE_CLASS="ibm-spectrum-scale-fileset"
info "Using storageClassName=${STORAGE_CLASS}"
```

- [ ] **Step 2: Add PVC bound check after creation**

After `create_pvc source-pvc 20Gi` (line 155), add:

```bash
# Wait for PVCs to bind
info "Waiting for PVCs to bind..."
for pvc_name in model-pvc data-pvc source-pvc; do
  kubectl wait --for=jsonpath='{.status.phase}'=Bound \
    pvc/"${pvc_name}" -n "${NAMESPACE}" --timeout=120s 2>/dev/null \
    && ok "PVC ${pvc_name} is Bound" \
    || warn "PVC ${pvc_name} not yet Bound — check storageClass and provisioner"
done
```

- [ ] **Step 3: Verify script parses**

Run: `bash -n .claude/skills/sim2real-setup/scripts/setup.sh`
Expected: no output (clean parse)

---

### Task 2: Fix setup.sh — Quay.io registry instructions

**Files:**
- Modify: `.claude/skills/sim2real-setup/scripts/setup.sh:177-200`

- [ ] **Step 1: Replace registry step with Quay-specific instructions**

Replace the Step 10 block (lines 177-200) with:

```bash
# ── Step 10: Container registry (Quay.io) ──────────────────────────
step 10 "Configuring container registry"
echo
echo -e "${BLUE}The EPP image will be pushed to a container registry.${NC}"
echo -e "${BLUE}If using Quay.io:${NC}"
echo "  1. Go to https://quay.io/repository/ and create a repository (e.g., llm-d-inference-scheduler)"
echo "  2. Go to Account Settings → Robot Accounts → Create Robot Account"
echo "  3. Grant the robot 'Write' permission on your repository"
echo "  4. Click the robot account name → Docker Configuration → download the auth JSON"
echo
if [ -z "${REGISTRY:-}" ]; then
  read -rp "Enter container registry (e.g., quay.io/username): " REGISTRY
fi
if [ -n "${REGISTRY}" ]; then
  info "Logging in to ${REGISTRY}..."
  ${CONTAINER_RT} login "${REGISTRY%%/*}" || warn "Registry login failed — you can retry later"

  # Look for auth config in standard locations
  CONFIG_PATH=""
  for candidate in \
    "${HOME}/.docker/config.json" \
    "${HOME}/.config/containers/auth.json" \
    "${XDG_RUNTIME_DIR:-/tmp}/containers/auth.json"; do
    if [ -f "${candidate}" ]; then
      CONFIG_PATH="${candidate}"
      break
    fi
  done

  if [ -n "${CONFIG_PATH}" ]; then
    kubectl create secret docker-registry registry-secret \
      --namespace "${NAMESPACE}" \
      --from-file=.dockerconfigjson="${CONFIG_PATH}" \
      --dry-run=client -o yaml | kubectl apply -f -
    ok "registry-secret created/updated for Kaniko builds"
  else
    warn "No container auth config found"
    echo "  Create registry-secret manually:"
    echo "    kubectl create secret docker-registry registry-secret \\"
    echo "      --namespace ${NAMESPACE} \\"
    echo "      --docker-server=quay.io \\"
    echo "      --docker-username=<robot-account-name> \\"
    echo "      --docker-password=<robot-account-token>"
  fi
else
  warn "No registry specified — skipping. Set later in config/env_defaults.yaml"
fi
```

- [ ] **Step 2: Verify script parses**

Run: `bash -n .claude/skills/sim2real-setup/scripts/setup.sh`

---

### Task 3: Fix setup.sh — Apply pipeline + OpenShift RBAC

**Files:**
- Modify: `.claude/skills/sim2real-setup/scripts/setup.sh:106-114` (RBAC)
- Modify: `.claude/skills/sim2real-setup/scripts/setup.sh:166-175` (Tekton deploy)

- [ ] **Step 1: Add cluster-admin role for helm-installer on OpenShift**

Replace the RBAC block (lines 106-115) with:

```bash
# ── Step 6: RBAC roles ─────────────────────────────────────────────
step 6 "Applying RBAC roles"
cd "${TEKTONC_DIR}"
envsubst '$NAMESPACE' < tekton/roles.yaml | kubectl apply -f -
if [ "$IS_OPENSHIFT" = true ]; then
  warn "OpenShift: adding SCC and cluster-admin policies"
  oc adm policy add-scc-to-user anyuid -z default -n "${NAMESPACE}" 2>/dev/null || true
  oc adm policy add-scc-to-user anyuid -z helm-installer -n "${NAMESPACE}" 2>/dev/null || true
  oc adm policy add-cluster-role-to-user cluster-admin -z helm-installer -n "${NAMESPACE}" 2>/dev/null || true
fi
ok "RBAC roles applied"
```

- [ ] **Step 2: Add pipeline apply after tasks**

Replace the Step 9 block (lines 166-175) with:

```bash
# ── Step 9: Deploy Tekton steps, tasks, and pipelines ──────────────
step 9 "Deploying Tekton steps, tasks, and pipelines"
cd "${TEKTONC_DIR}"
for f in tekton/steps/*.yaml; do
  [ -f "$f" ] && kubectl apply -f "$f" -n "${NAMESPACE}"
done
for f in tekton/tasks/*.yaml; do
  [ -f "$f" ] && kubectl apply -f "$f" -n "${NAMESPACE}"
done
# Apply pipeline definitions if any exist
for f in tekton/pipelines/*.yaml; do
  [ -f "$f" ] && kubectl apply -f "$f" -n "${NAMESPACE}"
done
# Also apply any compiled pipeline YAMLs in workspace
if [ -d "${SIM2REAL_ROOT}/workspace/tekton" ]; then
  for f in "${SIM2REAL_ROOT}"/workspace/tekton/pipeline-*.yaml; do
    [ -f "$f" ] && kubectl apply -f "$f" -n "${NAMESPACE}" && info "Applied $(basename "$f")"
  done
fi
ok "Tekton steps, tasks, and pipelines deployed"
```

- [ ] **Step 3: Verify script parses**

Run: `bash -n .claude/skills/sim2real-setup/scripts/setup.sh`

---

### Task 4: Fix setup.sh — Save outputs to file for downstream skills

**Files:**
- Modify: `.claude/skills/sim2real-setup/scripts/setup.sh:202-209`

- [ ] **Step 1: Write setup outputs to a JSON file**

Replace the "Done" block (lines 202-209) with:

```bash
# ── Save setup outputs ─────────────────────────────────────────────
SETUP_OUTPUT="${SIM2REAL_ROOT}/workspace/setup_config.json"
mkdir -p "${SIM2REAL_ROOT}/workspace"
cat > "${SETUP_OUTPUT}" <<SETUP_JSON
{
  "namespace": "${NAMESPACE}",
  "registry": "${REGISTRY:-}",
  "storage_class": "${STORAGE_CLASS}",
  "is_openshift": ${IS_OPENSHIFT},
  "tektonc_dir": "${TEKTONC_DIR}",
  "sim2real_root": "${SIM2REAL_ROOT}",
  "container_runtime": "${CONTAINER_RT}",
  "setup_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
SETUP_JSON
ok "Setup config saved to ${SETUP_OUTPUT}"

# ── Done ───────────────────────────────────────────────────────────
echo
echo -e "${GREEN}━━━ Setup complete ━━━${NC}"
echo
echo "Setup config saved to: ${SETUP_OUTPUT}"
echo
echo "Next steps:"
echo "  1. Review config/env_defaults.yaml (gateway, vLLM image, registry, fast_iteration)"
echo "  2. Run /sim2real-prepare to start the transfer pipeline"
echo
```

- [ ] **Step 2: Verify script parses**

Run: `bash -n .claude/skills/sim2real-setup/scripts/setup.sh`

---

### Task 5: Update setup SKILL.md — run inside Claude session

**Files:**
- Modify: `.claude/skills/sim2real-setup/SKILL.md`

- [ ] **Step 1: Update SKILL.md to instruct Claude to run the script itself**

Replace the current SKILL.md content (after frontmatter) with instructions that tell Claude to execute the script directly rather than asking the user to open a new terminal. The key change is adding `Edit` and `Write` to allowed-tools and instructing Claude to run the script via Bash tool with interactive prompts handled through the skill.

In the frontmatter, add `Edit` and `Write` to allowed-tools:

```yaml
allowed-tools:
  - Bash(**/setup.sh *)
  - Bash(kubectl *)
  - Bash(oc *)
  - Bash(tkn *)
  - Bash(python3 *)
  - Bash(pip *)
  - Bash(bash *)
  - Glob
  - Read
  - Edit
  - Write
```

In the body, replace the "Finding the Script" and "Usage" sections:

```markdown
## Execution

**Run the setup script directly inside this Claude session.** Do not ask the
user to open a separate terminal.

1. Locate the setup script:
   ```
   Glob: **/skills/sim2real-setup/scripts/setup.sh
   ```
   Store the result as `[SETUP_SCRIPT]`.

2. Collect required values from the user via questions (namespace, HF token)
   before running the script. Pass them as flags to avoid interactive prompts:
   ```bash
   bash [SETUP_SCRIPT] --namespace <NS> --hf-token <TOKEN> --registry <REG>
   ```

3. If the user doesn't provide a value, ask them using the AskUserQuestion
   tool before running the script.

4. After the script completes, read `workspace/setup_config.json` to confirm
   the outputs were saved. Report the summary to the user.
```

- [ ] **Step 2: Update the "What It Does" list to reflect new steps**

Add items 13 (save outputs) and note about pipeline apply. Update OpenShift note to include `cluster-admin`.

---

### Task 6: Switch AI review to use /review-plan from sdlc-plugins

**Files:**
- Modify: `.claude/skills/sim2real-prepare/SKILL.md:137-175`

- [ ] **Step 1: Replace the custom review script section with /review-plan usage**

Replace the "Stage 4: Multi-Model AI Review" section in `sim2real-prepare/SKILL.md` with:

```markdown
## Stage 4: Multi-Model AI Review

Use the existing `/review-plan` skill from sdlc-plugins for AI review.
This provides battle-tested LiteLLM integration with redaction and error handling.

### Prepare review content

Write a temporary review document combining the scorer code and context:

```bash
SCORER_FILE=$(python3 -c "import json; print(json.load(open('workspace/stage3_output.json'))['scorer_file'])")
EVOLVE_BLOCK=$(python3 -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['evolve_block_file'])")

cat > workspace/translation_review_input.md <<EOF
# Translation Consistency Review

## Task
Verify that the generated scorer Go code faithfully implements the evolved
routing algorithm. For EACH signal, check:
- The scorer reads the correct production field
- Normalization matches the algorithm summary
- Weight/coefficient matches the EVOLVE-BLOCK
- Scoring logic (comparison, threshold, combination) is faithful

Respond with: verdict (consistent/inconsistent), per-signal analysis, issues, suggestions.

## Generated Scorer Code
\`\`\`go
$(cat "$SCORER_FILE")
\`\`\`

## Algorithm Summary
\`\`\`json
$(cat workspace/algorithm_summary.json)
\`\`\`

## Signal Coverage
\`\`\`json
$(cat workspace/signal_coverage.json)
\`\`\`

## EVOLVE-BLOCK Source
\`\`\`go
$(cat "$EVOLVE_BLOCK")
\`\`\`
EOF
```

### Run reviews

Locate the review script:
```
Glob: **/skills/review-plan/scripts/review.sh
```
Store the result as `[REVIEW_SCRIPT]`.

For each round (1 to `REVIEW_ROUNDS`), run reviews with each model:

```bash
bash [REVIEW_SCRIPT] workspace/translation_review_input.md Azure/gpt-4o
bash [REVIEW_SCRIPT] workspace/translation_review_input.md GCP/gemini-2.5-flash
bash [REVIEW_SCRIPT] workspace/translation_review_input.md aws/claude-opus-4-6
```

### Evaluate reviews

Read each model's output. If any reviewer identifies inconsistencies:
- Apply fixes to the scorer file
- Re-validate build: `cd llm-d-inference-scheduler && GOWORK=off go build ./...`
- Regenerate `workspace/translation_review_input.md` with updated scorer
- Re-run reviews for the next round

After round N: if any reviewer still flags inconsistencies → **HALT**.

Save review results to `workspace/translation_reviews.json`.
```

---

### Task 7: Fix Opus/Gemini JSON parsing in review_translation.py

**Files:**
- Modify: `.claude/skills/sim2real-prepare/scripts/review_translation.py:59-109`

- [ ] **Step 1: Improve extract_json_from_content to handle Opus/Gemini quirks**

Replace the `extract_json_from_content` function (lines 59-109):

```python
def extract_json_from_content(content):
    """Extract JSON object from LLM response content.

    Handles common LLM output quirks:
    - Markdown code fences (```json ... ```)
    - Prose before/after JSON
    - Truncated responses (unclosed braces/brackets)
    - Opus: sometimes wraps JSON in explanation text
    - Gemini: sometimes uses single quotes or trailing commas
    """
    text = content.strip()

    # 1. Extract from code fences (greedy — take the largest fenced block)
    fence_matches = list(re.finditer(r'```(?:json)?\s*\n(.*?)```', text, re.DOTALL))
    if fence_matches:
        # Use the longest match (likely the actual JSON, not a nested example)
        text = max(fence_matches, key=lambda m: len(m.group(1))).group(1).strip()
    elif re.match(r'^```(?:json)?\s*\n', text):
        # Truncated fence: starts with ``` but no closing
        text = re.sub(r'^```(?:json)?\s*\n', '', text).strip()

    # 2. Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Find outermost { ... } (skip prose before/after)
    # Use a stack-based approach to find matching braces
    start = text.find('{')
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    end = start
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if depth == 0 and end > start:
        candidate = text[start:end]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Gemini fix: remove trailing commas before } or ]
        cleaned = re.sub(r',\s*([}\]])', r'\1', candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    # 4. Last resort: close unclosed braces
    candidate = text[start:]
    # Remove trailing commas
    candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
    open_braces = candidate.count('{') - candidate.count('}')
    open_brackets = candidate.count('[') - candidate.count(']')
    # Check if inside an unclosed string
    quote_count = len(re.findall(r'(?<!\\)"', candidate))
    if quote_count % 2 == 1:
        candidate += '"'
    candidate += ']' * max(0, open_brackets)
    candidate += '}' * max(0, open_braces)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    return None
```

- [ ] **Step 2: Verify script parses**

Run: `python3 -c "import ast; ast.parse(open('.claude/skills/sim2real-prepare/scripts/review_translation.py').read())"`

---

### Task 8: Fix deploy SKILL.md — namespace from setup + EPP naming

**Files:**
- Modify: `.claude/skills/sim2real-deploy/SKILL.md`

- [ ] **Step 1: Add setup config loading to prerequisites**

At the top of the Prerequisites section, before the artifact checks, add:

```markdown
## Loading Setup Config

Read `workspace/setup_config.json` (produced by `/sim2real-setup`) to get
namespace, registry, and other environment values:

```bash
NAMESPACE=$(python3 -c "import json; print(json.load(open('workspace/setup_config.json'))['namespace'])")
REGISTRY=$(python3 -c "import json; print(json.load(open('workspace/setup_config.json'))['registry'])")
export NAMESPACE
```

If `workspace/setup_config.json` doesn't exist, prompt the user for `NAMESPACE`.
```

- [ ] **Step 2: Fix EPP image naming in Stage 3 (Build EPP)**

In the "Resolve image tag" section, replace the tag/naming logic:

```markdown
2. Resolve image tag:
   ```bash
   GIT_SHA=$(cd llm-d-inference-scheduler && git rev-parse --short HEAD)
   ALG_NAME=$(python3 -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['algorithm_name'])")
   TAG="${ALG_NAME}-${GIT_SHA}"
   REGISTRY_HUB=$(python3 -c "import yaml; d=yaml.safe_load(open('config/env_defaults.yaml')); print(d['stack']['gaie']['epp_image']['build']['hub'])")
   IMAGE_NAME="llm-d-inference-scheduler"
   FULL_IMAGE="${REGISTRY_HUB}/${IMAGE_NAME}:${TAG}"
   ```

This produces tags like `evolved_router-a1b2c3d` instead of `sim2real-a1b2c3d`,
making it clear which algorithm the image contains.
```

- [ ] **Step 3: Ensure Kaniko pod uses NAMESPACE from setup config**

In the Kaniko pod spec, the `${NAMESPACE}` variable is already used. Verify the
prerequisite section sets it from `setup_config.json` so the build happens in
the correct namespace.

---

### Task 9: Update sim2real-prepare SKILL.md — load setup config

**Files:**
- Modify: `.claude/skills/sim2real-prepare/SKILL.md`

- [ ] **Step 1: Add setup config loading to prerequisites**

After the "Record the pipeline commit" block, add:

```markdown
### Load Setup Config

If `workspace/setup_config.json` exists (from `/sim2real-setup`), load it:

```bash
if [ -f workspace/setup_config.json ]; then
  NAMESPACE=$(python3 -c "import json; print(json.load(open('workspace/setup_config.json'))['namespace'])")
  REGISTRY=$(python3 -c "import json; print(json.load(open('workspace/setup_config.json'))['registry'])")
  export NAMESPACE
fi
```

This ensures values from setup are available during prepare.
```

---

### Task 10: Verify all changes

- [ ] **Step 1: Verify setup.sh parses**

```bash
bash -n /Users/jchen/go/src/inference-sim/sim2real/.claude/skills/sim2real-setup/scripts/setup.sh
```
Expected: clean (no output)

- [ ] **Step 2: Verify Python scripts parse**

```bash
python3 -c "import ast; ast.parse(open('/Users/jchen/go/src/inference-sim/sim2real/.claude/skills/sim2real-prepare/scripts/review_translation.py').read())"
```
Expected: clean (no output)

- [ ] **Step 3: Verify no main repo files changed**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
git diff --name-only HEAD -- . ':!.claude' ':!docs/superpowers'
```
Expected: only pre-existing changes (tekton/roles.yaml etc.)

- [ ] **Step 4: List all modified skill files**

```bash
find /Users/jchen/go/src/inference-sim/sim2real/.claude/skills/sim2real-* -type f | sort
```

Expected:
```
.claude/skills/sim2real-deploy/SKILL.md
.claude/skills/sim2real-prepare/scripts/build_review_request.py
.claude/skills/sim2real-prepare/scripts/review_translation.py
.claude/skills/sim2real-prepare/SKILL.md
.claude/skills/sim2real-setup/scripts/setup.sh
.claude/skills/sim2real-setup/SKILL.md
```
