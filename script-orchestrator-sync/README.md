# script-orchestrator-sync

Sync utility that reads rows from a source sheet, applies mapping rules from a control sheet, and writes into a target sheet through a Sheets-JSON gateway.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

## Performance Mode

Pipeline v1 now supports two edit modes:
- `found_footage` (existing behavior)
- `performance` (new)

`performance` supports two sub-modes:
- `simple_sync_cut`: one full performance clip + master audio, then per-cut short exports.
- `preset`: 2-6 labeled clips (`a`-`f`) cut by a performance preset DSL.

Render mode fields are read from config row (compiler input):
- `Render Mode`: `found_footage | performance` (default: `found_footage`)
- `Performance Mode`: `simple_sync_cut | preset` (optional when `Render Mode=performance`)
- `Performance Master Clip File ID` (required for `simple_sync_cut`)
- `Performance Clip A File ID` ... `Performance Clip F File ID` (optional explicit mapping for `preset`)
- `Performance Preset` (fallback preset DSL for `preset` mode)
- `Performance Source Folder ID` (optional; default: `YOUR_GOOGLE_SHEET_ID`)

Compiler fallback behavior for performance mode:
- If `Performance Mode` is blank and any performance preset DSL exists (config fallback or Executive Preset column B), compiler auto-infers `preset`.
- If `Performance Mode` is blank and no performance preset DSL exists, compiler uses `simple_sync_cut`.
- In inferred/explicit `preset` mode, if no explicit A-F clip ids are provided, compiler auto-maps labels from the first 2-6 video files in `Performance Source Folder ID` (deterministic filename order).

Compatibility aliases accepted by compiler input:
- config-style (`Render Mode`, `Performance Mode`, `Performance Preset`, etc.)
- snake_case (`render_mode`, `performance_mode`, `performance_preset_dsl`, etc.)

Performance clip resolution behavior:
- Drive ids and Drive URLs are accepted directly.
- Non-id clip refs are resolved by exact filename inside `Performance Source Folder ID`.
- Name resolution can be disabled with `PERFORMANCE_CLIP_NAME_RESOLUTION=0` (id/url only mode).

Executive preset sheet columns (`Executive Preset File ID`) are interpreted as:
- Column A: found-footage stitch preset (`FF Presets`)
- Column B: performance preset DSL (`Performance Presets`)
- Column C: text/lyrics preset (`Lyric Presets`)

Performance preset DSL grammar:
- Repeated tokens: `<duration><clip><modifiers>`
- `duration`: positive integer
- `clip`: one letter `a`..`f`
- `modifiers`: optional `r` (reverse), `b` (black flash), `w` (white flash)
- Example: `4a2br2cw`

Performance mode config template (single data row example):

```text
Render Mode = performance
Performance Mode = preset
Performance Master Clip File ID = 
Performance Clip A File ID = 1abc...A
Performance Clip B File ID = 1abc...B
Performance Clip C File ID = 1abc...C
Performance Clip D File ID = 1abc...D
Performance Clip E File ID = 
Performance Clip F File ID = 
Performance Preset = 4a2br2cw
Audio File ID = 1audio...
lyrics .srt File ID = 1lyrics...
copy. srt File ID = 1copy...   # optional
Cut Clips? = TRUE
Clip 1 = TRUE
Clip 1 Start = 00:00:12:44
Clip 1 End = 00:00:24:48
Clip 2 = TRUE
Clip 2 Start = 00:00:26:12
Clip 2 End = 00:00:32:24
```

Manifest schema:
- `schema_version: pipeline_v1@2`
- top-level `edit_mode`
- top-level `performance` block for performance-specific settings

`Render_Jobs` now includes trailing columns:
- `edit_mode`
- `performance_mode`
- `performance_preset`

## Deep Observability + Forensics

Pipeline v1 now supports structured run telemetry and auto-forensics on failure.

Default high-detail envs (see `.env.prod.example`):
- `PIPELINE_OBSERVABILITY_MODE=deep`
- `PIPELINE_JSON_EVENTS_ENABLED=1`
- `PIPELINE_STAGE_HEARTBEAT_SECONDS=20`
- `PIPELINE_STAGE_TIMEOUT_SECONDS=0`
- `PIPELINE_FORENSICS_ON_FAILURE=1`
- `PIPELINE_FORENSICS_FRAME_SAMPLES=12`
- `PIPELINE_FORENSICS_UPLOAD=1`

Generated artifacts:
- `logs/events.jsonl` per run
- `diagnostics/<run_id>/summary.json` and `diagnostics/<run_id>/bundle.zip` on failures

Quick preflight before running a batch:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
.venv_local/bin/python scripts/trigger_render_batch.py --preflight-only
# or: make run-batch-preflight
```

Watch live structured events:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
.venv_local/bin/python scripts/watch_render_events.py --follow
```

If you need to reduce overhead temporarily:
- `PIPELINE_OBSERVABILITY_MODE=basic`
- `PIPELINE_FORENSICS_ON_FAILURE=0`

## Containerized Dev (Hot Reload + Local Editing)

This repo now includes a dev-first container setup that keeps editing/debugging easy:
- source is bind-mounted (`../` -> `/workspace`) so local file edits are live in-container
- API runs with `uvicorn --reload`
- sibling repos are addressable in-container:
  - `/workspace/vse-stitcher`
  - `/workspace/vse-text`
- Docker daemon (Docker Desktop) must be running before `docker compose` commands.
- Image install now includes:
  - pinned Blender runtime (`BLENDER_VERSION`, default `4.2.2`)
  - `ffmpeg`
  - bundled copies of `script-orchestrator-sync`, `vse-stitcher`, and `vse-text`
- Container startup checks now fail fast on missing requirements:
  - `CONTROL_SHEET_ID`
  - missing `GOOGLE_APPLICATION_CREDENTIALS` file (if env var is set)
  - missing render binaries/scripts (unless `REQUIRE_RENDER_BINARIES=0`)

From `script-orchestrator-sync/`:

```bash
docker compose build app
docker compose up app
```

API will be on `http://localhost:8000`.

One-off shell / batch in the same containerized environment:

```bash
make docker-shell
CONTROL_SHEET_ID=<your_control_sheet_id> make docker-run-batch
```

Credential/default path behavior in compose:
- host gcloud dir is mounted read-only: `~/.config/gcloud -> /workspace/.config/gcloud`
- default credential path in container:
  `/workspace/.config/gcloud/application_default_credentials.json`
- override with env var if needed:
  `GOOGLE_APPLICATION_CREDENTIALS=/path/in/container/key.json`
- Optional Blender pin override during image build:
  `BLENDER_VERSION=4.2.2 docker compose build app`

VM-style runtime (no reload, same Dockerfile):

```bash
docker build -t script-orchestrator-sync:vm -f Dockerfile ..
docker run --rm -p 8000:8000 \
  -e CONTROL_SHEET_ID=<your_control_sheet_id> \
  -e SHEETS_JSON_BASE_URL=https://YOUR-SHEETS-JSON-GATEWAY.a.run.app \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/key.json \
  -v /path/to/key.json:/secrets/key.json:ro \
  script-orchestrator-sync:vm
```

Production compose (recommended on VM):

```bash
cp .env.prod.example .env.prod
# edit .env.prod
make docker-build-prod
make docker-up-prod
```

Containerized VM batch-worker (isolated from API/local flow):

```bash
cp .env.vm.example .env.vm
# edit .env.vm (recommended: RENDER_EXEC_BACKEND=local)
make docker-build-vm-batch
make docker-up-vm-batch
make docker-logs-vm-batch
```

Batch-worker runtime notes:
- Uses `docker-compose.prod.yml` + `docker-compose.vm-batch.yml`.
- Loops `trigger_render_batch.py` with preflight each cycle.
- Keeps current local non-container render workflow unchanged.
- Defaults to local in-container render backend via `.env.vm` (`RENDER_EXEC_BACKEND=local`).
- Loop controls (in `.env.vm`): `BATCH_LOOP_INTERVAL_SECONDS`, `BATCH_LOOP_EXIT_ON_ERROR`, `BATCH_LOOP_ONCE`.

Production compose defaults:
- mounts `./work` for runtime artifacts/logs generated by app scripts
- mounts `./secrets` read-only at `/secrets` for optional key-file auth
- rotates container logs (`10m` x `5` files)

Auth modes on VM:
- recommended: VM attached service account + leave `GOOGLE_APPLICATION_CREDENTIALS` empty
- key-file mode: set `GOOGLE_APPLICATION_CREDENTIALS=/secrets/google-application-credentials.json`
  and place the JSON file at `./secrets/google-application-credentials.json`

VM update/deploy command (pull + build + restart + health check):

```bash
make deploy-vm
```

VM batch-worker deploy command (pull + build + restart worker, optionally API):

```bash
make deploy-vm-batch
```

`deploy-vm-batch` defaults to restarting both `app` and `batch-worker`.
Set `DEPLOY_WITH_API=0` to restart worker only.

The deploy script (`scripts/deploy_vm.sh`) does:
- `git fetch/pull` (fast-forward only)
- `docker compose -f docker-compose.prod.yml build app`
- `docker compose ... up -d app`
- polls `http://127.0.0.1:${APP_PORT:-8000}/healthz` until healthy

## Legacy Cloud VM Runbook (GitHub + GCP South Carolina)

1. Push your deployment branch to GitHub:

```bash
cd /Users/samrooker/Desktop/sf-production-app
git checkout -b codex/cloud-vm-prep
git add .gitignore \
  .dockerignore \
  script-orchestrator-sync/docker-compose.prod.yml \
  script-orchestrator-sync/.env.prod.example \
  script-orchestrator-sync/Makefile \
  script-orchestrator-sync/scripts/bootstrap_vm.sh \
  script-orchestrator-sync/README.md
git commit -m "Prepare production container runtime for GCP VM"
git push -u origin codex/cloud-vm-prep
```

2. Create the VM in South Carolina (`us-east1`). Example with `gcloud`:

```bash
gcloud compute instances create sf-prod-sync-vm \
  --project <PROJECT_ID> \
  --zone us-east1-c \
  --machine-type e2-standard-4 \
  --image-family debian-12 \
  --image-project debian-cloud \
  --boot-disk-size 80GB \
  --boot-disk-type pd-balanced \
  --tags http-server,https-server
```

3. SSH into the VM and bootstrap:

```bash
gcloud compute ssh sf-prod-sync-vm --zone us-east1-c --project <PROJECT_ID>
```

```bash
git clone https://github.com/metrothenews/sf-production-app.git /opt/sf-production-app
cd /opt/sf-production-app/script-orchestrator-sync
bash scripts/bootstrap_vm.sh
```

4. Configure app env and auth on VM:
- edit `/opt/sf-production-app/script-orchestrator-sync/.env.prod`
- edit `/opt/sf-production-app/script-orchestrator-sync/.env.vm` (set `RENDER_EXEC_BACKEND=local`)
- set `CONTROL_SHEET_ID` and any optional overrides
- auth option A (recommended): leave `GOOGLE_APPLICATION_CREDENTIALS=` blank and use VM service account
- auth option B: copy key JSON to
  `/opt/sf-production-app/script-orchestrator-sync/secrets/google-application-credentials.json`
  and set
  `GOOGLE_APPLICATION_CREDENTIALS=/secrets/google-application-credentials.json`

5. Start or update the container on VM:

```bash
cd /opt/sf-production-app/script-orchestrator-sync
make deploy-vm
curl -fsS http://127.0.0.1:${APP_PORT:-8000}/healthz
```

Start or update the VM batch-worker:

```bash
cd /opt/sf-production-app/script-orchestrator-sync
make deploy-vm-batch
make docker-logs-vm-batch
```

6. Optional: expose publicly only after local health is green:
- open VM firewall / VPC rules for your chosen `APP_PORT` (default `8000`)
- then verify from outside VM:

```bash
curl -fsS http://<VM_EXTERNAL_IP>:8000/healthz
```

## Cloud Render Update Workflow (Current Production Path)

Use this flow for production cloud rendering updates.

Current production components:
- Control plane: Cloud Run service `so-control-api`
- Render workers: ephemeral Cloud Batch VMs (created per job, then terminated)
- Region: `us-east1` (South Carolina)
- Artifact Registry repo: `us-east1-docker.pkg.dev/drive-cdn-proxy/so-images`

### If someone asks about "the VM"

Use this exact clarification:
- "Production render does not use one persistent VM. It uses Cloud Batch, which spins up short-lived worker VMs per render job and tears them down after completion. Cloud Run (`so-control-api`) submits and tracks those jobs."

How to verify that quickly:

```bash
gcloud run services describe so-control-api \
  --region us-east1 \
  --project drive-cdn-proxy \
  --format='value(spec.template.spec.containers[0].env,status.url)'
```

Look for:
- `RENDER_EXEC_BACKEND=batch`
- `BATCH_IMAGE_URI=<artifact-registry-image>`

Note:
- The "VM batch-worker" compose setup in this README is a separate self-hosted deployment option.
- It is not the same as current Cloud Run + Cloud Batch production.

### Exact rollout + verification sequence (the one used in production on March 18, 2026)

1) Confirm auth + project:

```bash
gcloud auth list
gcloud config get-value project
```

Expected project: `drive-cdn-proxy`.

2) Build and push a new worker image using the checked-in Cloud Build config:

```bash
cd /Users/samrooker/Desktop/sf-production-app
TS=$(date +%Y%m%d-%H%M%S)
IMAGE="us-east1-docker.pkg.dev/drive-cdn-proxy/so-images/script-orchestrator-sync:${TS}"
gcloud builds submit . \
  --config script-orchestrator-sync/cloudbuild.batch-image.yaml \
  --substitutions _IMAGE_URI="${IMAGE}" \
  --project drive-cdn-proxy
echo "${IMAGE}"
```

3) Update Cloud Run env so new Batch jobs use the new worker image:

```bash
gcloud run services update so-control-api \
  --region us-east1 \
  --project drive-cdn-proxy \
  --update-env-vars "BATCH_IMAGE_URI=${IMAGE}"
```

4) Verify Cloud Run revision/env after update:

```bash
gcloud run services describe so-control-api \
  --region us-east1 \
  --project drive-cdn-proxy \
  --format='value(status.latestReadyRevisionName,spec.template.spec.containers[0].image,spec.template.spec.containers[0].env)'
```

Important:
- For worker-only rollout, Cloud Run container image may stay on an older tag.
- That is expected as long as `BATCH_IMAGE_URI` is updated to the new tag.

5) Trigger a real render batch:

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -sS -X POST "https://YOUR-CONTROL-API.a.run.app/api/v1/render/run-batch" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"poll_seconds":1.0,"max_rounds":40}'
```

Capture `batch_job_name` from the response (example: `projects/drive-cdn-proxy/locations/us-east1/jobs/render-...`).

6) Confirm the spawned Batch job is using the expected image tag:

```bash
JOB_ID="<render-job-id-from-response>"
gcloud batch jobs describe "${JOB_ID}" \
  --location us-east1 \
  --project drive-cdn-proxy \
  --format='value(status.state,taskGroups[0].taskSpec.runnables[0].container.imageUri)'
```

7) Monitor until terminal state:

```bash
gcloud batch jobs describe "${JOB_ID}" \
  --location us-east1 \
  --project drive-cdn-proxy \
  --format='value(status.state,status.runDuration,status.statusEvents[-1].description)'
```

8) Pull run logs:

```bash
gcloud logging read \
  "logName=\"projects/drive-cdn-proxy/logs/batch_task_logs\" AND labels.task_group_name:\"${JOB_ID}\"" \
  --project drive-cdn-proxy \
  --limit=200 \
  --order=desc \
  --format='value(timestamp,severity,textPayload)'
```

9) Confirm API-side final status:

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -sS "https://YOUR-CONTROL-API.a.run.app/api/v1/render/batch-jobs/${JOB_ID}" \
  -H "Authorization: Bearer ${TOKEN}"
```

Expected healthy result:
- `status: success`
- `batch_state: succeeded`
- task logs show `Render run success: ...`

### Full control-plane + worker rollout (only when API code changed)

If `so-control-api` code changed too, update both Cloud Run image and worker image:

```bash
gcloud run services update so-control-api \
  --region us-east1 \
  --project drive-cdn-proxy \
  --image "${IMAGE}" \
  --update-env-vars "^##^BATCH_IMAGE_URI=${IMAGE}##BATCH_SERVICE_ACCOUNT_SCOPES=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive" \
  --set-secrets DRIVE_SA_KEY_JSON=so-drive-sa-key-json:latest
```

### Rollback (fast)

Rollback worker image only:

```bash
PREV_IMAGE="<previous-good-image-uri>"
gcloud run services update so-control-api \
  --region us-east1 \
  --project drive-cdn-proxy \
  --update-env-vars "BATCH_IMAGE_URI=${PREV_IMAGE}"
```

Rollback both API image and worker image:

```bash
PREV_IMAGE="<previous-good-image-uri>"
gcloud run services update so-control-api \
  --region us-east1 \
  --project drive-cdn-proxy \
  --image "${PREV_IMAGE}" \
  --update-env-vars "BATCH_IMAGE_URI=${PREV_IMAGE}"
```

### Cost safety defaults to keep

- Keep `BATCH_JOB_QUEUE=render-default` and serial trigger behavior (`409 busy` when active).
- Keep Cloud Run `min-instances=0`, `max-instances=1`, `concurrency=1`.
- Keep Batch retries low (`BATCH_MAX_RETRIES=1`) until stable.
- Keep billing budget alerts and Batch failure alerts enabled.

## Clip Library Mirror (Drive -> GCS)

Goal:
- Keep clip-library curation in Google Drive.
- Avoid repeated Drive download costs during renders by mirroring clips into GCS.
- Render pipeline reads clips from GCS mirror (`gcs_mirror` mode) instead of pulling clip media from Drive each run.

Key mode behavior:
- Default compile/render source is `drive` unless you explicitly set `CLIPS_LIBRARY_SOURCE=gcs_mirror`.
- Mirror freshness is warn-only (`CLIPS_LIBRARY_MIRROR_WARN_STALE_HOURS`).
- Missing mirrored clip object at render time is hard-fail (no clip fallback to Drive).

Recommended operator lifecycle:
1. Curate clip folders/files in Drive (`collection-<number>` structure).
2. Run mirror refresh (`make library-mirror-refresh-dry`, then `make library-mirror-refresh`).
3. Run compile/render in `gcs_mirror` mode.
4. If needed, rollback source with `CLIPS_LIBRARY_SOURCE=drive`.

Mirror refresh command:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
export CLIPS_LIBRARY_FOLDER_ID=YOUR_GOOGLE_SHEET_ID
export CLIPS_LIBRARY_MIRROR_BUCKET=<your-bucket>
export CLIPS_LIBRARY_MIRROR_PREFIX=library-mirror
export CLIPS_LIBRARY_MIRROR_QUARANTINE_TTL_DAYS=7
make library-mirror-refresh
```

Dry run:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
export CLIPS_LIBRARY_FOLDER_ID=YOUR_GOOGLE_SHEET_ID
export CLIPS_LIBRARY_MIRROR_BUCKET=<your-bucket>
export CLIPS_LIBRARY_MIRROR_PREFIX=library-mirror
make library-mirror-refresh-dry
```

What `library_mirror_refresh` does:
- scans direct Drive subfolders matching `collection-<number>` only
- recursively indexes video files and inferred category (`P/C/F/T/U/M`)
- uploads new/mismatched clips to `gs://<bucket>/<prefix>/clips/...`
- moves stale active clips to quarantine (`gs://<bucket>/<prefix>/quarantine/<timestamp>/...`)
- purges quarantined clips after `CLIPS_LIBRARY_MIRROR_QUARANTINE_TTL_DAYS` (default `7`)
- writes mirror index JSON at:
  `gs://<bucket>/<prefix>/indexes/library_mirror_index.json`
- tracks quarantine state at:
  `gs://<bucket>/<prefix>/indexes/quarantine_manifest.json`

Switch render compile to GCS mirror source:

```bash
export CLIPS_LIBRARY_SOURCE=gcs_mirror
export CLIPS_LIBRARY_MIRROR_BUCKET=<your-bucket>
export CLIPS_LIBRARY_MIRROR_PREFIX=library-mirror
export CLIPS_LIBRARY_MIRROR_INDEX_OBJECT=library-mirror/indexes/library_mirror_index.json
export CLIPS_LIBRARY_MIRROR_WARN_STALE_HOURS=24
```

Compile + run with mirror source:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
make run-batch-preflight
make run-batch
```

In `gcs_mirror` mode:
- `compile_config_to_render_job.py` reads clip inventory from mirror index JSON.
- compile logs mirror freshness (`generated_utc`, age hours) and warns if stale.
- compile validates category coverage for selected collections/cut presets before queueing jobs.
- manifest clip rows carry `gcs_uri`/`gcs_object`.
- `render_agent.py` downloads clip media from GCS for those clip rows.
- missing GCS clip/object fails the run immediately (no Drive fallback for clip media).
- audio/lyrics/copy assets still use Drive IDs (unchanged).

Rollback:

```bash
export CLIPS_LIBRARY_SOURCE=drive
```

Troubleshooting signals:
- Compile warning: `mirror_freshness_warn ...` means index is older than warning threshold.
- Compile hard-fail: `Mirror category coverage check failed before queueing render job ...`
- Render hard-fail: GCS clip download errors are terminal for the run (`clip_download_source=gcs ...` followed by error).

## Happy Path Entrypoints

- FastAPI: `src/so_sync/api.py`
  - `POST /api/v1/jobs/{job_id}/run`
  - `GET /api/v1/runs/{run_id}`
  - `POST /api/v1/render/run-batch`
- Sync engine: `src/so_sync/sync_engine.py` (`execute_job`)
- Repositories + sheet client:
  - `src/so_sync/repositories.py`
  - `src/so_sync/gateway.py`
- Contract/header helpers: `src/so_sync/sheet_contract.py`

## Smoke Test

Run a deterministic end-to-end smoke test directly against the sync engine (no HTTP API involved):

```bash
make smoke-dry
```

Required env vars:
- `CONTROL_SHEET_ID`

Optional env vars:
- `SHEETS_JSON_BASE_URL` (default: `https://YOUR-SHEETS-JSON-GATEWAY.a.run.app`)
- `SHEETS_JSON_API_KEY`
- `JOB_ID` (optional override; if unset, runner auto-selects the single enabled job in `Sync_Jobs`)
- `DRY_RUN=1` (executes full run lifecycle but skips target writes)
- `DEDUPE_KEY` (otherwise auto-generated)

Make targets:
- `make healthcheck`: fast preflight for control sheet readability and enabled-job invariant
- `make healthcheck-render`: enforce render tab/header preflight
- `make bootstrap-render`: idempotent render tab header bootstrap
- `make smoke-dry`: bootstrap + dry-run smoke
- `make smoke`: bootstrap + dry-run smoke + live smoke + idempotency smoke
- `make render-smoke`: bootstrap render tabs then run render agent once

`make` targets fail fast if `CONTROL_SHEET_ID` is missing.

Smoke assertions:
- A run is created in `Runs_Ledger` with `run_id`.
- Lifecycle transitions include `queued -> running -> success|failed`.
- `source_fingerprint` and `dedupe_key` are populated.
- If not `DRY_RUN`, target writes are observed and read back with header-aligned row width.

## Bootstrapping The Control Sheet

Bootstrap control tabs + headers and seed a sample job/mapping:

```bash
python3 scripts/bootstrap_control_sheet.py
```

Required env vars:
- `CONTROL_SHEET_ID`

Optional env vars:
- `SHEETS_JSON_BASE_URL` (default: `https://YOUR-SHEETS-JSON-GATEWAY.a.run.app`)
- `SHEETS_JSON_API_KEY`
- `INCLUDE_ACCESS_CONTROL=1` (also bootstrap `Access_Control`)
- `SAMPLE_SOURCE_SHEET_ID`
- `SAMPLE_TARGET_SHEET_ID`
- `SAMPLE_SOURCE_TAB`
- `SAMPLE_TARGET_TAB`

Behavior:
- Requires tabs `Sync_Jobs`, `Mapping`, and `Runs_Ledger` to already exist.
- Ensures headers for those tabs match the contract.
- Ensures a canonical sample job row exists: `job_id=default`, `enabled=TRUE`.
- Migrates legacy sample job `sample_sync_intake_to_so` to `default` when needed.
- Writes minimal sample mapping rows for `job_id=default` if missing.
- Idempotent:
  - exact matching headers => no-op
  - mismatched headers => fail loud with column-by-column diff

## Live Verification Checklist

- Ensure gateway allowlist + share are correct:
  - add `CONTROL_SHEET_ID` (and source/target sheet IDs) to `SHEETS_ALLOWLIST`
  - share sheets with `sheets-db-service@latest-ave-sheets-01.iam.gserviceaccount.com`
- In the Google Sheets UI, create tabs named exactly: `Sync_Jobs`, `Mapping`, `Runs_Ledger`.
- Run `python3 scripts/print_control_sheet_id.py` to confirm the active `CONTROL_SHEET_ID`.
- Run `make healthcheck`.
- Run `make smoke`.
- Success signals:
  - log contains `Selected job_id=default source=auto-selection` (when `JOB_ID` is unset)
  - `Runs_Ledger` has a successful run entry
  - repeat live run does not produce duplicate target writes

## Integration Fixture Spreadsheet

- Duplicate your control spreadsheet in Google Sheets (`File` -> `Make a copy`) and use that copy as test fixture.
- Add the new fixture sheet id to `SHEETS_ALLOWLIST` (comma-separated, no spaces).
- Share the fixture sheet with `sheets-db-service@latest-ave-sheets-01.iam.gserviceaccount.com`.
- Run smoke against fixture:

```bash
CONTROL_SHEET_ID=<fixture_sheet_id> make smoke
```

## SO Campaigns Migration (Staging -> Production)

This migration updates only `SO Campaigns` with non-breaking performance-mode columns.

Target sheets:
- staging: `YOUR_GOOGLE_SHEET_ID`
- production: `YOUR_GOOGLE_SHEET_ID`

New trailing columns added by migration:
- `render_mode`
- `performance_mode`
- `performance_audio_policy`
- `performance_master_clip_drive_file_id`
- `performance_clip_a_drive_file_id`
- `performance_clip_b_drive_file_id`
- `performance_clip_c_drive_file_id`
- `performance_clip_d_drive_file_id`
- `performance_clip_e_drive_file_id`
- `performance_clip_f_drive_file_id`
- `performance_preset_dsl`

Backfill compatibility defaults:
- if `render_mode` empty and `edit_mode=FF_ONLY`: set `render_mode=found_footage`
- if `render_mode` empty and `edit_mode=FF_PLUS_PERF`: set `render_mode=performance`
- if `render_mode=performance` and `performance_mode` empty: set `simple_sync_cut`
- if `render_mode=performance` and `performance_audio_policy` empty: set `master_only`

Read-only audit:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
SHEET_ID=YOUR_GOOGLE_SHEET_ID \
PROD_TAB='SO Campaigns' \
SAMPLE_ROWS=25 \
python3 scripts/audit_so_campaigns_schema.py
```

Dry-run migration:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
SHEET_ID=YOUR_GOOGLE_SHEET_ID \
PROD_TAB='SO Campaigns' \
APPLY=0 \
python3 scripts/migrate_so_campaigns_performance_mode.py
```

Apply migration:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync
SHEET_ID=YOUR_GOOGLE_SHEET_ID \
PROD_TAB='SO Campaigns' \
APPLY=1 \
python3 scripts/migrate_so_campaigns_performance_mode.py
```

Production run uses the same commands with:
- `SHEET_ID=YOUR_GOOGLE_SHEET_ID`

Promotion model:
- run full audit + dry-run + apply sequence on staging
- repeat on production
- keep staging as pre-prod for future schema/data changes

Rollback:
- this migration is append-only for headers and non-destructive for non-empty cells
- rollback path is value-level correction (re-run migration with adjusted defaults or update target cells)
- no column deletion/reorder is performed by scripts

Dual-write intake sync:
- `scripts/sync_intake_to_so.py` now writes legacy fields and new performance-mode fields together
- `performance_mode` defaults to `preset` when a performance preset exists, otherwise `simple_sync_cut`
- clip-id columns are only written when explicit intake values are provided
- intake default tab is now `mode` with a leftmost `Mode` column (`found footage | performance | hybrid`)
- when `INTAKE_TAB=mode` (or legacy `api-read`), the script routes to:
  - `found-footage-mode`
  - `performance-footage-mode`
  - `hybrid-mode`
  and reads that mode tab row as the primary source (with non-empty control-tab fallback during migration)

## Render Jobs v0 (noop renderer)

Required tabs in the CONTROL sheet:
- `Render_Jobs`
- `Outputs_Index`
- `Render_Runs`

Create these tabs in Google Sheets UI first, then bootstrap headers:

```bash
CONTROL_SHEET_ID=<your_control_sheet_id> make bootstrap-render
```

Optional preflight:

```bash
CONTROL_SHEET_ID=<your_control_sheet_id> make healthcheck-render
```

Example `Render_Jobs` row values for one noop job:
- `render_job_id`: `render_noop_001`
- `enabled`: `TRUE`
- `priority`: `10`
- `status`: `queued`
- `attempts`: `0`
- `max_attempts`: `3`
- `campaign_id`: `demo_campaign_001`
- `input_mode`: `local`
- `renderer`: `noop`
- `output_mode`: `local`
- `created_utc`: current UTC timestamp

Additional `Render_Jobs` fields for `renderer=vse_text` local-only execution:
- `input_video_path`: absolute local path to input video
- `input_audio_path`: absolute local path to input audio
- `input_srt_path`: absolute local path to input `.srt`
- `bg_mode`: value passed to `--bg-mode`
- `output_local_path`: absolute local path for rendered `.mp4`

Additional `Render_Jobs` fields for `renderer=pipeline_v1`:
- `stitch_preset`: preset value passed to the stitcher stage
- `bpm`: stitch BPM used by beat-based cut timing (populated by config compiler from `Song BPM`)
- `seed`: deterministic stitcher seed
- `copy_srt_drive_file_id`: optional Drive file id for copy-overlay SRT

Run render smoke:

```bash
CONTROL_SHEET_ID=<your_control_sheet_id> make render-smoke
```

## Config Compiler (Sheet -> Render_Jobs + Manifest)

Compile one config row from the source spreadsheet into:
- a Drive-hosted `pipeline_v1` manifest JSON
- one queued `Render_Jobs` row (idempotent upsert by deterministic `render_job_id`)
- stitch BPM sourced from config (`Song BPM`) and propagated to both manifest (`stitch.bpm`) and
  `Render_Jobs.bpm`
- clip metadata that preserves inferred category tags on each manifest clip (`P/C/F/T/U/M`, etc.)
- BPM source priority:
  1) `Song BPM` (config sheet)
  2) `BPM` / `Stitch BPM` (legacy/alternate config columns)
  3) `PIPELINE_STITCH_BPM` env var
  4) default `120`

Run:

```bash
CONTROL_SHEET_ID=<your_control_sheet_id> make compile-config
```

Optional env vars:
- `CONFIG_SHEET_ID` (default: `YOUR_GOOGLE_SHEET_ID`)
- `CONFIG_TAB` (default: `mode`)
- `CONFIG_RANGE` (default: `A1:ZZ2`)
- `CLIPS_LIBRARY_FOLDER_ID` (default: `YOUR_GOOGLE_SHEET_ID`)
- `OUTPUT_PARENT_FOLDER_ID` (default: `YOUR_GOOGLE_SHEET_ID`)
- `ASSETS_FOLDER_ID` (optional folder used for filename->Drive id lookup)
- `PERFORMANCE_SOURCE_FOLDER_ID` (default: `YOUR_GOOGLE_SHEET_ID`)
- `PERFORMANCE_CLIP_NAME_RESOLUTION` (default: `1`; set `0` to require ids/urls for performance clips)
- `FOUND_FOOTAGE_MODE_TAB` (default: `found-footage-mode`)
- `PERFORMANCE_MODE_TAB` (default: `performance-footage-mode`)
- `HYBRID_MODE_TAB` (default: `hybrid-mode`)
- `MODE_CONTROL_TAB` (default: `mode`; legacy `api-read` is also accepted)
- `PIPELINE_STITCH_BPM` (fallback BPM when config has no BPM value; default `120`)

When `CONFIG_TAB=mode` (or legacy `api-read`), compiler reads leftmost `Mode` and routes to the matching
mode tab above, using mode-tab values as primary and control-tab values as non-empty fallback.

Category inference used by compiler:
- Folder/file names are scanned for common tokens and normalized to category keys.
- Built-in mappings include: `people/person -> P`, `cars/car -> C`, `fireworks/firework -> F`,
  `urban/city -> U`, `money/cash -> M`, `travel/trip -> T`.
- If no keyword match exists, the first alphabetic token initial is used.

Song title overlay fields (config row):
- `Display Song Name?`: when `TRUE`, render a static song-title overlay.
- `Song Name`: required when `Display Song Name?` is `TRUE`.
- Behavior: title is rendered with the same font family/style as lyric captions, at `60%` of caption size,
  centered near the top black bar, and held for the full rendered duration.

## One-Command Batch Run (Compile + Queue Drain + Progress)

Run a full batch from current config sheet state with one command:

```bash
CONTROL_SHEET_ID=<your_control_sheet_id> make run-batch
```

What this command does:
- acquires a local lock so duplicate clicks/runs do not overlap
- checks `Render_Jobs` and exits with `batch_busy=true` if an enabled row is already `leased` or `running`
- runs `scripts/compile_config_to_render_job.py`
- drains queued/retry jobs by calling `scripts/render_agent.py run-once` for each queued render job id
- computes and prints queue-level progress snapshots:
  - `queue_progress={"total_jobs":...,"done_jobs":...,"percent":...}`

Optional progress-sheet write-through:
- `PROGRESS_SHEET_ID`: sheet id to write progress percent into
- `PROGRESS_RANGE`: A1 range where the percent value should be written (single cell)

Optional resilience tuning for Blender stages:
- `PIPELINE_BLENDER_STAGE_RETRIES` (default: `1`) extra retries for `pipeline_v1` stitch/lyrics/copy stages
- `PIPELINE_BLENDER_RETRY_DELAY_SECONDS` (default: `1`) delay between retries
- `BLENDER_STAGE_RETRIES` / `BLENDER_STAGE_RETRY_DELAY_SECONDS` (same behavior for standalone `vse_text` / `vse_stitcher` renderers)
- `PIPELINE_PER_CUT_RENDER` (default: `1`) keeps `pipeline_v1` in per-cut render mode (current build requires `1`)
- `PIPELINE_EFFECTS_PASS2_ENABLED` (default: `1`) enables two-pass stitch flow: `stitch(base_only) -> effects(pass2) -> lyrics/copy`
- `PIPELINE_EFFECTS_PASS2_STRICT` (default: `1`) fail cut/run when effects pass contract checks fail
- `PIPELINE_EFFECTS_PASS2_KEEP_PLAN_ON_SUCCESS` (default: `0`) keep per-cut effects plan JSON locally after success
- `BLENDER_BIN` (default: auto-detect mac path, otherwise `blender`)
- `VSE_STITCHER_SCRIPT_PATH` / `VSE_EFFECTS_SCRIPT_PATH` / `VSE_TEXT_SCRIPT_PATH` (override external script locations; defaults resolve from workspace sibling repos)

When both vars are set, each progress snapshot also writes `percent` (0-100) into that sheet cell.

Render horizon cap (important):
- In per-cut mode, each cut trims audio/SRT to that cut window first, then each short is rendered directly to that cut duration.
- The pipeline no longer relies on full-length render-then-cut video behavior for `pipeline_v1`.

Preset/category behavior in `pipeline_v1`:
- Per-cut stitch presets are honored directly from `manifest.cuts[*].stitch_preset`.
- Stitch preset grammar is `digits + optional modifiers + category`, where category is always the final letter.
- Allowed modifiers: `b`, `w`, `s`, `sb`, `sw` (empty modifier means normal behavior).
- Example extended preset: `4p4sbc4f4t2sp2u2c2wm`.
- Category letters in preset DSL (for example `4p4c4f4t2p2u2c2m`) map to category-specific clip pools.
- Random selection is constrained to the current category only.
- Clips do not repeat within a category until that category pool is exhausted, then the pool reshuffles.
- Missing required categories now fail loudly instead of silently falling back to mixed/random buckets.
- Legacy manifests with uncategorized clips still run: uncategorized clips are used as fallback pools.
- Two-pass modifier contract:
  - Stitch pass renders base media only and writes a deterministic effects plan sidecar.
  - Effects pass applies frame-level replacements from the sidecar before text overlay.
  - `b` / `w`: replace frame 1 of that shot with black/white (shot duration unchanged).
  - `s`: interlace two different clips frame-by-frame.
  - `sb` / `sw`: interlace color and one clip frame-by-frame (color-first).
  - If `s` has insufficient unique clips, planner marks fallback-to-normal for that segment and emits a warning.

Subtitle behavior in `pipeline_v1` per-cut mode:
- Per-cut SRT slicing now auto-normalizes Resolve-style hour-offset cues (`01:00:00,000+`) before overlap checks.
- SRT time parsing accepts both comma and dot millisecond separators.
- Global lyric timing includes a temporary `150ms` advance (`HARDCODED_LYRIC_ADVANCE_MS`) to bring on-screen lyrics earlier.
- SRT text normalization preserves intended line breaks (`<br>`, `\\N`, `\\n`) and wraps overlong lines in `linebreak_mode="srt"` to avoid off-screen overflow.
- Edge cues that land exactly on cut boundaries are kept as tiny clips to avoid dropped first/last lyric words.
- If per-cut mode finds zero lyric cues across all cuts while the main lyrics SRT is non-empty, the run fails loudly.
- Auto-speed reveal (preset-driven): when `reveal.auto_speed.enabled=true`, word reveal speed is computed per cue from `words/second`.
- Auto-speed knobs: `reveal.auto_speed.baseline_quantile`, `max_speedup`, `gamma`, `min_words_for_baseline`, `fallback_baseline_wps`.
- Per-cut baseline anchoring: in per-cut `pipeline_v1`, the first lyric-bearing cut computes the auto-speed baseline and later cuts reuse that same baseline.
  This keeps verse timing stable in `clip_001` while allowing chorus cuts (for example `clip_002`) to accelerate deterministically.
- Renderer override hook: `vse-text` supports `--auto-baseline-wps <float>` (plumbed by render agent when anchoring baseline across cuts).
- Behavior contract: sparse cues reveal faster (up to `max_speedup`) then hold full cue text until cue end; dense cues stay near `1x`.

API trigger (for button integration) uses the same underlying flow:

```bash
curl -X POST "http://localhost:8000/api/v1/render/run-batch" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Response status behavior:
- `200` with `status=started|success|noop`
- `409` when blocked by in-flight process (`status=busy`)
- `500` when the batch fails (`status=error`)

Drive auth note for batch/compile:
- If Drive proxy is unavailable, compiler/agent fallback uses Drive API directly.
- Set `GOOGLE_APPLICATION_CREDENTIALS` to a Drive-enabled credential JSON with access to required files/folders.

Cut time format in config row must be `HH:MM:SS:FF` where `FF` is hundredths (00-99).
The compiler converts to ffmpeg timestamps with milliseconds using `ms = FF * 10`.

## Runbook: Manual `make run-batch` Setup

Use this when running from terminal on a local machine.

1. Use user ADC credentials (Drive write quota available):
   - expected path: `/Users/<you>/.config/gcloud/application_default_credentials.json`
2. Confirm config/control sheet ids are correct.
3. Run from repo root:

```bash
cd /Users/samrooker/Desktop/sf-production-app/script-orchestrator-sync && \
CONTROL_SHEET_ID=YOUR_GOOGLE_SHEET_ID \
GOOGLE_APPLICATION_CREDENTIALS=/Users/samrooker/.config/gcloud/application_default_credentials.json \
make run-batch
```

Notes:
- This command compiles current config row(s), then drains queued render jobs.
- A run can finish local renders before Drive uploads complete. If the parent batch process is interrupted, local MP4s may exist under
  `so_work/<run_id>/outputs` while no new file appears in Drive yet.
- If using a service-account key JSON instead of user ADC, Drive uploads may fail with
  `storageQuotaExceeded` unless you are writing to a properly configured Shared Drive.

Local retention controls (both pipeline and local render-agent flows):
- `PIPELINE_UPLOAD_VERIFY_MODE=size` (default): after upload, confirms Drive file metadata exists and `size` matches local bytes.
- `PIPELINE_LOCAL_CLEANUP_ON_SUCCESS=1` (default): removes local run working files after successful completion.
- `PIPELINE_LOCAL_CLEANUP_ON_FAILURE=logs_only` (default): on failure, deletes local media/intermediates and keeps only logs.

### Clip Collection Folder Behavior

- Collections are auto-discovered from direct subfolders under `CLIPS_LIBRARY_FOLDER_ID` (default root:
  `YOUR_GOOGLE_SHEET_ID`) with names matching exactly `collection-<number>`.
- Subfolders with any other naming pattern are ignored (for example `old-collection-1`).
- New matching folders (for example `collection-8`) are picked up automatically with no config changes.
- Each short output clip is assigned a random collection, and the selector enforces no consecutive repeats
  across the full batch.
- Collection assignment is per cut clip, not per full-length run.
- Each selected collection folder is scanned recursively (up to `CLIPS_LIBRARY_MAX_DEPTH`, default `4`)
  when building that collection's pool.

Success signals:
- Render job transitions through `leased` -> `running` -> `success`
- On terminal transitions (`success`/`failed`), lease fields are cleared: `lease_owner=""`, `lease_until_utc=""`
- Failure retries are explicit: `attempts` increments, `last_error_utc` is stamped, status becomes `retry` until `attempts >= max_attempts`, then `failed`
- `Render_Runs` has a row with `run_type=render` and terminal `status=success`
- `Outputs_Index` has a new row with `artifact_type=noop`
- If no job is leased, agent logs one-line diagnostics with enabled count, eligible count (`queued`/`retry`), and a sample of `(render_job_id,status,enabled)`

## Operational Gotchas

- `403` / allowlist failure:
  - Add sheet id to `SHEETS_ALLOWLIST` (comma-separated, no spaces).
  - Share sheets with: `sheets-db-service@latest-ave-sheets-01.iam.gserviceaccount.com`
- `Unable to parse range`:
  - Usually a tab-name or A1 mismatch. Quote tab names with spaces, e.g. `'SO Campaigns'!A1:Z`.
- Bad request shape:
  - Read must use `{sheet_id, ranges:[...]}`.
  - Write must use `{sheet_id, updates:[{range, values}]}`.

## Render Agent Runner

Run one or more end-to-end render cycles:

```bash
PYTHONPATH=src python3 -m render_agent.run --limit 1
```

Options:
- `--job-id <render_job_id>`: lease only that job id
- `--dry-run`: print runnable queued/retry job ids and exit
- `--smoke`: alias for `--limit 1`

Drive operator notes:
- Output folder ID: `YOUR_GOOGLE_SHEET_ID`
- Share input files/folders with proxy service account:
  `drive-cdn-proxy@drive-cdn-proxy.iam.gserviceaccount.com`
- Proxy test:
  ```bash
  curl -I "https://YOUR-DRIVE-PROXY.a.run.app/f/<FILE_ID>"
  ```
- A proxy `404` usually means the file/folder is not shared with the proxy service account (or wrong ID).

Seed one known-good render job row:

```bash
PYTHONPATH=src .venv_local/bin/python scripts/seed_one_render_job.py \
  --job-id cfg_273b8cd57385 \
  --source-folder-id <FOLDER_ID_WITH_VIDEOS> \
  --target-count 5 \
  --status queued \
  --select-mode shuffle_seeded \
  --select-seed 12345 \
  --output-folder-id YOUR_GOOGLE_SHEET_ID
```

## Local Artifact Prune Utility

One-time cleanup helper for stale local render artifacts under temp `so_work/` and repo `work/`.

Dry-run (default behavior):
```bash
PYTHONPATH=src .venv_local/bin/python scripts/prune_local_render_artifacts.py --dry-run
```

Apply deletion:
```bash
PYTHONPATH=src .venv_local/bin/python scripts/prune_local_render_artifacts.py --apply
```

Keep only `logs/` under run directories:
```bash
PYTHONPATH=src .venv_local/bin/python scripts/prune_local_render_artifacts.py --apply --failure-logs-only
```

Permissions reminder:
- Source files/folders must be shared with:
  `drive-cdn-proxy@drive-cdn-proxy.iam.gserviceaccount.com`
- Output folder write access must be granted to the local agent credentials from `GOOGLE_APPLICATION_CREDENTIALS`.
