# cohdi-ci — CoHDI component CI

This repository hosts a self-hosted CI environment that builds, deploys, and
tests the three [CoHDI](https://github.com/CoHDI) components
(`composable-dra-driver`, `composable-resource-operator`,
`dynamic-device-scaler`) on a small test cluster with a mock CDI backend. It
is a dedicated repo, separate from the components themselves, because it
carries environment-specific details such as internal IPs.

## Environment

Three instances:

| Instance        | Role                                                              |
|-----------------|-------------------------------------------------------------------|
| `ci-management` | Hosts the GitHub Actions self-hosted runner and a local Docker container registry (`10.128.0.21:5000`) |
| `ci-cp`         | Kubernetes control-plane node. CoHDI components, `mock-server`, and DRA driver controllers run here |
| `ci-worker`     | Kubernetes worker node with an NVIDIA Tesla T4 GPU attached |

`ci-cp` and `ci-worker` are normally kept **stopped** between test sessions.
Run `startCIInstances.yaml` to start them and `stopCIInstances.yaml` to stop
them.

### Key component versions (as of 2026-10-02)

| Component | Version |
|---|---|
| Kubernetes (apiserver/kubelet) | v1.37.0 |
| Node OS / kernel | RHEL 10.2 / 6.12.0-211.61.1.el10_2 |
| containerd | 2.2.5 |
| GPU Operator | v26.3.3 |
| NVIDIA driver | 580.159.04 (rhel10) |
| NVIDIA DRA Driver (`dra-driver-nvidia-gpu`) | v0.4.0 |
| flannel | v0.28.1 |
| Helm | v3.20.0 |
| Docker / registry | 29.6.2 / `registry:2` |
| mock-server | `mock_cohdi:test3` |
| cohdi-chart (CoHDI/cohdi-chart) | v0.2.2 (pinned via `CHART_REF`) |

> **Caution (k8s 1.37 vs the CRO)**: k8s 1.37 removed the
> `resource.k8s.io/v1alpha3` API, which the CRO's detach path
> (`DeviceTaintRule`) still uses, so the kube-apiserver runs with the
> `--runtime-config=resource.k8s.io/v1alpha3=true` workaround (check with
> `kubectl -n kube-system get pod kube-apiserver-ci-cp -o yaml | grep runtime-config`).
> This is currently being addressed, and the workaround is planned to be removed
> once the fix is complete.

### The registry
`ci-management` runs a `registry:2` container on port 5000 (HTTPS, self-signed
cert), reachable as `10.128.0.21:5000`.
`cohdi-component-deploy.yaml` builds and pushes here, so that a normal `helm upgrade` +
`imagePullPolicy` pull is enough to get the image onto whichever node the pod
lands on.

## Setup requirements

- **Runner prerequisites**: `ci-management` needs `gcloud` (authenticated),
  `kubectl`, `helm`, `docker`, and `gh`
  ([GitHub CLI](https://github.com/cli/cli/blob/trunk/docs/install_linux.md);
  used for the API calls inside the workflows)
- **Self-hosted runner**: register the `ci-management` runner **to this
  repository only**, not to the public component repos (a fork PR could
  otherwise run third-party code on the runner)
- **GitHub App** (org-owned): needs **Actions: write** (on this repo) for the
  trigger to dispatch the pipeline, and **Issues: write / Pull requests:
  write / Commit statuses: write** (on the component repos) for the
  pipeline's label, comment, and commit-status write-back.
- **Secrets**: on this repo, `KUBECONFIG_CICDVM` (the cluster's admin
  kubeconfig), `COHDI_CRO_WEBHOOK_CERTS` (the CRO webhook TLS material; kept
  out of git because it contains a private key), and the App's ID/private
  key. Each component repo also holds the App's ID/private key (for the
  trigger's dispatch)
- **Trigger placement**: put `trigger-cohdi-ci.yaml` on each component
  repo's **default branch** (`pull_request_target` only fires from the
  definition on the default branch), and create the `run-cohdi-ci` label in
  each repo
- **Public-repo caveats**: Actions run logs are world-readable. Secret
  values are masked automatically, but anything a step echoes is visible, so
  keep sensitive cluster details out of diagnostic output. Set fork-PR
  workflow approval to "require for all outside collaborators" and protect
  `main` with required reviews
- **CRO support for k8s 1.37**: see the caution above (the apiserver
  workaround must be applied)

## Operational flow

1. A PR is opened on a component repo (`composable-dra-driver` / `composable-resource-operator` / `dynamic-device-scaler`)
2. For a fork PR from an outside contributor, the `pull_request`-triggered workflows sit **awaiting approval**. Normally that is the component repo's own CI (the Go unit tests in `ci.yaml`), but **what actually runs is defined by the PR's head**, so a maintainer reviews the diff — including any workflow changes — clicks "Approve and run", and checks the results. This approval releases only the `pull_request`-triggered workflows; the CoHDI CI does not run yet at this point
3. A maintainer adds the **`run-cohdi-ci`** label to the PR → the CI test (full pipeline) runs; on success the **`cohdi-ci-passed`** label is added
4. Once `cohdi-ci-passed` has been added, do the code review
5. If the review finds no problems, merge (merging is always done manually)

## About change PRs to the cohdi-ci repository

A PR that changes cohdi-ci's own workflows or values should be exercised by CI before merging. The flow up to running CI depends on where the PR comes from.

- **PR from someone with write access to this repo**: push the working branch directly to this repo and dispatch the pipeline against it (`workflow_dispatch` runs the definition from the named branch, so no default-branch switch is needed).
- **Fork PR (outside contributors)**: fork workflows do not run automatically, so a maintainer runs it by:
  1. Reviewing the fork PR's diff (especially `.github/workflows/`, Dockerfiles, and secret handling — a safety check before running unreviewed code on the runner)
  2. If it looks safe, creating a branch on this repo and pushing the fork PR's content to it
  3. Dispatching the pipeline against that branch and checking the result
  4. If green, doing the code review and merging

### Running the pipeline against a specific branch

- **CLI**:
  ```bash
  gh workflow run cohdi-component-deploy-and-test.yaml \
    --ref <branch> \
    -f component=<component> \
    -f pr_number=<PR number to test>
  ```
- **GUI**: Actions tab -> "CoHDI Component Deploy and Test" -> "Run workflow" -> pick the branch under "Use workflow from", fill in `component` and `pr_number`, and run.

Either way, running requires the maintain role or higher.

## About PRs that change the trigger (`trigger-cohdi-ci.yaml`) in the three component repositories

The triggers placed in [composable-dra-driver](https://github.com/CoHDI/composable-dra-driver), [dynamic-device-scaler](https://github.com/CoHDI/dynamic-device-scaler), and [composable-resource-operator](https://github.com/CoHDI/composable-resource-operator) are identical in content, and they run on `pull_request_target`, which is **fixed to the default branch (main)**.
So a change needs three PRs, and the trigger cannot be run through CI before merging.

Accordingly, a PR against the trigger follows this flow:

0. Open a PR against each of the three component repositories
1. **Diff review** — check for added dangerous events, checkout of PR code, weakened actor gates, or changes to secret handling
2. If the review passes, merge, then **run a post-merge smoke test with a dummy PR** to confirm it works
3. If something is wrong, fix it and open the PR again

## Workflows

### PR trigger (lives in each component repo)
- **`trigger-cohdi-ci.yaml`** — lives not in this repo but in **each of the three component repos**. The CI can be run with nothing but a **label on the PR**:
  - A maintainer adds the **`run-cohdi-ci`** label to the PR, and the CI runs (`pull_request_target`, `labeled` event).
  - The labeler's role is verified: below maintain, nothing is dispatched — the label is removed and the run fails with an error
  - If the check passes, it dispatches `cohdi-component-deploy-and-test.yaml` in this repo via the App token and **removes the `run-cohdi-ci` label** (note: the label is a request, not a state)
  - The pipeline posts a **start comment** on the PR (tested head SHA + the run's URL) and adds the **`cohdi-ci-passed`** label only on success. Failure adds no label — the verdict for anything else is the run itself
  - **New commits pushed to the PR (`synchronize` event) remove `cohdi-ci-passed` automatically** — the pass label always means "the **current** head passed"
  - Note that a successful trigger run only means the dispatch succeeded

### Instance lifecycle
- **`startCIInstances.yaml`** / **`stopCIInstances.yaml`** — start/stop `ci-cp` and `ci-worker`. Callable both directly and as reusable workflows (`workflow_call`); `cohdi-component-deploy-and-test.yaml` calls both automatically (see below), so manual use is not needed.

### Registry cleanup
- **`cohdi-registry-cleanup.yaml`** — disk cleanup on `ci-management` (prunes the Docker build cache and old images). Manual-only by design — it never runs automatically; run it when disk usage gets high (~75% as a guideline). It shares the pipeline's concurrency group, so it can never run in the middle of a deploy

### Single-component build & deploy
- **`cohdi-component-deploy.yaml`** — builds one component from source (`component` input: `composable-dra-driver` / `composable-resource-operator` / `dynamic-device-scaler`), pushes the image to the `ci-management` registry, and deploys the full stack in one `helm upgrade` with all three component images pinned explicitly: the target at the given tag/ref, the other two guaranteed to be at the current tip of their own `main` (rebuilt from source if the deployed tag is stale). The cohdi-chart used for the deploy is pinned to `v0.2.2` via `CHART_REF` in `cohdi-component-deploy.yaml` (clean-cloned every run, resolved commit logged); bump it with a PR that raises `CHART_REF`.

  Inputs:
  - `component` (required)
  - `image_tag` (required) — the tag to assign to the built image; **must be unique per build** (e.g. include the commit SHA)
  - `build_ref` (formally optional, but see below) — git ref to build (a branch name, tag, commit SHA, or `refs/pull/<N>/head`); empty means the tip of the component repo's default branch (`main`). **When testing a PR it must be set** (PR head SHA or `refs/pull/<N>/head`) — the tag is just a label, `build_ref` is what decides which code gets built

### Testing
- **`cohdi-test-cicdvm.yaml`** — the GPU pod deploy test. Restarts `mock-server`, cleans up leftover test resources, deploys a test Pod requesting a GPU via DRA, verifies attach, deletes the Pod, and verifies detach.
- **`cohdi-component-deploy-and-test.yaml`** — the main end-to-end entry point, and the single operational entrance. It starts the instances, waits for the cluster/CNI to be stable, brings the other two components up to their current `main`, builds and deploys the target, runs the GPU pod test, and stops the instances via `if: always()`. All runs serialize through one concurrency group (`cohdi-component-deploy-test`), so overlapping PR tests never fight over the test environment

The Helm values are managed in this repo under `cohdi-ci/values/`
(only the CRO webhook TLS block is split out into the
`COHDI_CRO_WEBHOOK_CERTS` secret).

## Usage
**Test a change to a component:**
- **Testing a PR (label flow — the normal flow)**: a maintainer adds the `run-cohdi-ci` label to the PR (see the trigger section above). Role check → dispatch → PR-head resolution → baseline guarantee for the other two components → build & deploy of the PR's code → GPU pod test → instance stop → `cohdi-ci-passed` on success, all automatic. Once the green label is on, reviewers review, approve, and merge.
- **Testing a PR (direct pipeline dispatch)**: running `cohdi-component-deploy-and-test.yaml` in this repo with `component` + `pr_number` does the same thing. The actor's role is verified on the pipeline side as well.
- **Testing an arbitrary ref**: same workflow with `image_tag` (unique per build) and optional `build_ref` instead of `pr_number`.

## Known issues
- **`cro-node-agent` is a stub.** The DaemonSet deployed by the chart's
  `deployPrivilegedAgent: true` just runs `busybox` / `sleep infinity`; the
  GPU removal it is meant to perform on detach (`rmmod` + removal from the
  PCI bus) never happens. One of the causes of "GPU detach can take ~10+
  minutes" below.
- **`ci-worker`'s GPU is real, local hardware, not fabric-attached.** Its
  Tesla T4 is always physically attached. The
  test Pod's `ResourceClaim` lists `node-gpu` (matches local hardware)
  before `fabric-gpu` (goes through `composable-resource-operator`'s mock-CDI
  attach flow), so as long as the local GPU is visible, tests exercise the
  `node-gpu` path and never actually create a `ComposabilityRequest` —
  `dynamic-device-scaler`'s attach/detach duration env vars
  (`DEVICE_NO_REMOVAL_DURATION` etc.) have no effect on this path.
- **GPU detach can take ~10+ minutes.** With the above two points combined,
  "detach" in the pod test actually means: NVIDIA's own
  `dra-driver-nvidia-gpu-kubelet-plugin` DaemonSet stops reporting the local
  GPU, which only happens once Node Feature Discovery flips the
  `nvidia.com/gpu.present` label to `false` on `ci-worker` — a process on the
  order of many minutes, not something tunable via CoHDI config.
