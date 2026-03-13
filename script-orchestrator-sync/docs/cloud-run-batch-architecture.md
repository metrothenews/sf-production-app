# Cloud Run Trigger + Cloud Batch Renderer

## Goal
Run heavy Blender/ffmpeg rendering only when needed, while keeping the current Sheets/Drive orchestration flow and avoiding an always-on render VM.

## Architecture Overview
1. Cloud Run service (`so-control-api`) hosts the API trigger endpoints.
2. `POST /api/v1/render/run-batch` checks for active render jobs in `Render_Jobs`.
3. If active jobs exist (`leased`/`running`), API returns `409 busy`.
4. If no active jobs exist, API submits one Cloud Batch job.
5. Cloud Batch starts one VM task and runs:
   `python scripts/trigger_render_batch.py --poll-seconds <n> --max-rounds <n>`
6. Batch worker runs existing compiler + render agent flow, uploads outputs, updates sheets, and exits.
7. API returns metadata (`batch_job_name`, `batch_region`, `submitted_utc`) for tracking.

## Current Code Paths
- API backend switch:
  - `RENDER_EXEC_BACKEND=local|batch`
  - file: `src/so_sync/api.py`
- Batch request construction and status normalization:
  - file: `src/so_sync/batch_trigger.py`
- Existing local subprocess flow (still available for dev fallback):
  - file: `src/so_sync/render_batch_trigger.py`
- Batch worker credential materialization:
  - file: `scripts/batch_entrypoint.sh`

## Runtime Environment Contract
These variables are used by the batch-backed API flow.

Required for batch mode:
- `RENDER_EXEC_BACKEND=batch`
- `GCP_PROJECT_ID=<project-id>`
- `GCP_REGION=us-east1`
- `BATCH_IMAGE_URI=<artifact-registry-image>`
- `CONTROL_SHEET_ID=<sheet-id>`

Recommended defaults:
- `BATCH_JOB_QUEUE=render-default`
- `BATCH_MACHINE_TYPE=n2d-standard-8`
- `BATCH_SERVICE_ACCOUNT_EMAIL=so-render-batch@<project-id>.iam.gserviceaccount.com`
- `BATCH_MAX_RETRIES=1`
- `BATCH_TIMEOUT_SECONDS=14400`
- `BATCH_TASK_CPU_MILLI=8000`
- `BATCH_TASK_MEMORY_MIB=32768`

Credential materialization:
- `DRIVE_SA_KEY_JSON=<json secret payload>`
- `DRIVE_SA_KEY_FILE=/secrets/drive-sa.json`
- `GOOGLE_APPLICATION_CREDENTIALS` is set to `DRIVE_SA_KEY_FILE` during worker startup.

## IAM Model
Cloud Run service account:
- `roles/batch.jobsEditor`
- `roles/logging.logWriter`
- (if reading secrets directly in service) `roles/secretmanager.secretAccessor`

Batch runtime service account:
- `roles/logging.logWriter`
- `roles/secretmanager.secretAccessor` (if retrieving secrets at runtime)
- `roles/artifactregistry.reader` (to pull private container images)

## Request/Response Contract
Endpoint: `POST /api/v1/render/run-batch`

Request fields (unchanged):
- `poll_seconds`
- `max_rounds`
- `startup_wait_seconds` (used by local backend only)

Response includes:
- `status` (`started|busy|noop|error`)
- `execution_backend` (`local|batch`)
- `batch_job_name` (batch backend)
- `batch_region` (batch backend)
- `submitted_utc`
- `correlation_id`

Status endpoint:
- `GET /api/v1/render/batch-jobs/{batch_job_name}`
- Returns normalized Batch lifecycle state (`running|success|failed`) and raw batch state.

## Deployment Runbook (Manual CLI)
All examples use region `us-east1`.

1. Build and push image:
```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project <PROJECT_ID>

gcloud artifacts repositories create so-images \
  --repository-format=docker \
  --location=us-east1 \
  --description="SO render images"

IMAGE="us-east1-docker.pkg.dev/<PROJECT_ID>/so-images/script-orchestrator-sync:$(date +%Y%m%d-%H%M%S)"
gcloud auth configure-docker us-east1-docker.pkg.dev
docker build -t "${IMAGE}" -f script-orchestrator-sync/Dockerfile /Users/samrooker/Desktop/sf-production-app
docker push "${IMAGE}"
```

2. Create service accounts:
```bash
gcloud iam service-accounts create so-control-api
gcloud iam service-accounts create so-render-batch
```

3. Grant API SA permissions:
```bash
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:so-control-api@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/batch.jobsEditor"
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:so-control-api@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter"
gcloud iam service-accounts add-iam-policy-binding \
  "so-render-batch@<PROJECT_ID>.iam.gserviceaccount.com" \
  --member="serviceAccount:so-control-api@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

4. Grant Batch runtime SA permissions:
```bash
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:so-render-batch@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:so-render-batch@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:so-render-batch@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.reader"
```

5. (If needed) create/update secret:
```bash
printf '%s' '<SERVICE_ACCOUNT_JSON>' | gcloud secrets create so-drive-sa-key-json --data-file=-
# If secret already exists:
# printf '%s' '<SERVICE_ACCOUNT_JSON>' | gcloud secrets versions add so-drive-sa-key-json --data-file=-
```

6. Deploy Cloud Run API:
```bash
gcloud run deploy so-control-api \
  --image "${IMAGE}" \
  --region us-east1 \
  --service-account "so-control-api@<PROJECT_ID>.iam.gserviceaccount.com" \
  --min-instances 0 \
  --max-instances 1 \
  --concurrency 1 \
  --set-env-vars "RENDER_EXEC_BACKEND=batch,GCP_PROJECT_ID=<PROJECT_ID>,GCP_REGION=us-east1,BATCH_IMAGE_URI=${IMAGE},BATCH_MACHINE_TYPE=n2d-standard-8,BATCH_SERVICE_ACCOUNT_EMAIL=so-render-batch@<PROJECT_ID>.iam.gserviceaccount.com,BATCH_MAX_RETRIES=1,BATCH_TIMEOUT_SECONDS=14400,BATCH_TASK_CPU_MILLI=8000,BATCH_TASK_MEMORY_MIB=32768,BATCH_JOB_QUEUE=render-default,CONTROL_SHEET_ID=<CONTROL_SHEET_ID>,SHEETS_JSON_BASE_URL=https://YOUR-SHEETS-JSON-GATEWAY.a.run.app,DRIVE_SA_KEY_FILE=/secrets/drive-sa.json" \
  --set-secrets "DRIVE_SA_KEY_JSON=so-drive-sa-key-json:latest"
```

7. Trigger a render batch:
```bash
curl -X POST "https://<RUN_URL>/api/v1/render/run-batch" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -d '{"poll_seconds":1.0,"max_rounds":40}'
```

8. Check status:
```bash
curl "https://<RUN_URL>/api/v1/render/batch-jobs/<batch-job-id-or-full-name>" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)"
```

## Failure Handling Notes
- Duplicate triggers while active jobs exist return `409 busy`.
- Batch submit failures return `500` with `correlation_id`.
- Missing batch config (`GCP_PROJECT_ID`, `BATCH_IMAGE_URI`) returns `500` with clear error.
- Worker-side auth issues fail fast in `scripts/batch_entrypoint.sh`/render script startup.

## Rollback
To roll back quickly to in-container local subprocess flow:
```bash
gcloud run services update so-control-api \
  --region us-east1 \
  --set-env-vars "RENDER_EXEC_BACKEND=local"
```

## Testing Checklist
Unit:
- `tests/test_batch_trigger.py`
- `tests/test_api_render_run_batch.py`

Staging smoke:
1. Trigger with no queued work and confirm API returns predictable status.
2. Trigger with queued work and confirm Batch job creation.
3. Trigger twice quickly and verify second call returns `409 busy`.
4. Validate sheet updates (`Render_Jobs`, `Render_Runs`, `Outputs_Index`) after successful run.
