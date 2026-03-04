# script-orchestrator-sync

Sync utility that reads rows from a source sheet, applies mapping rules from a control sheet, and writes into a target sheet through a Sheets-JSON gateway.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

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

Run:

```bash
CONTROL_SHEET_ID=<your_control_sheet_id> make compile-config
```

Optional env vars:
- `CONFIG_SHEET_ID` (default: `YOUR_GOOGLE_SHEET_ID`)
- `CONFIG_TAB` (default: `Sheet1`)
- `CONFIG_RANGE` (default: `A1:AZ2`)
- `CLIPS_LIBRARY_FOLDER_ID` (default: `YOUR_GOOGLE_SHEET_ID`)
- `OUTPUT_PARENT_FOLDER_ID` (default: `YOUR_GOOGLE_SHEET_ID`)
- `ASSETS_FOLDER_ID` (optional folder used for filename->Drive id lookup)

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
- `PIPELINE_RENDER_END_PADDING_SECONDS` (default: `1.0`) adds safety padding to the cut horizon cap used by `pipeline_v1` render stages
- `BLENDER_BIN` (default: auto-detect mac path, otherwise `blender`)
- `VSE_STITCHER_SCRIPT_PATH` / `VSE_TEXT_SCRIPT_PATH` (override external script locations; defaults resolve from workspace sibling repos)

When both vars are set, each progress snapshot also writes `percent` (0-100) into that sheet cell.

Render horizon cap (important):
- `pipeline_v1` now computes `max_cut_end_s` from manifest cuts and caps stage render length to:
  - `render_end_seconds = max_cut_end_s + PIPELINE_RENDER_END_PADDING_SECONDS`
- The cap is passed to stitch/lyrics/copy Blender scripts via `--render-end-seconds`.
- This prevents rendering the unused tail of long source audio/video when requested cuts are short.

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
- If using a service-account key JSON instead of user ADC, Drive uploads may fail with
  `storageQuotaExceeded` unless you are writing to a properly configured Shared Drive.

### Clip Collection Folder Behavior

- Collection toggles are read from config keys matching `V Collection N` (for example:
  `V Collection 1`, `V Collection 2`, `V Collection 3`, `V Collection 4`).
- Each enabled collection resolves to its own folder under `CLIPS_LIBRARY_FOLDER_ID` (default root:
  `YOUR_GOOGLE_SHEET_ID`), by matching direct subfolder names like:
  `V Collection N` or `Collection N`.
- Optional explicit folder-id columns are supported and take precedence, for example:
  `V Collection 2 Folder ID`.
- Clip pools stay isolated per collection (no mixing).
- When multiple collections are enabled, compiler alternates by run:
  `r01 -> Collection 1`, `r02 -> Collection 2`, `r03 -> Collection 3`, `r04 -> Collection 4`, then repeat.
- Alternation is per full-length render run only (not per cut clip). All cut clips produced from a single
  run use that run's selected collection.
- Example: if 2 cut clips are enabled and 17 videos are requested, compiler creates 9 runs and assigns
  collections as `1,2,3,4,1,2,3,4,1`; the final run is trimmed to output only one cut clip.
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

Permissions reminder:
- Source files/folders must be shared with:
  `drive-cdn-proxy@drive-cdn-proxy.iam.gserviceaccount.com`
- Output folder write access must be granted to the local agent credentials from `GOOGLE_APPLICATION_CREDENTIALS`.
