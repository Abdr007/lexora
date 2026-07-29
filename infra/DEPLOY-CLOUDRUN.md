# Deploying Lexora to GCP Cloud Run

End to end, from nothing to a public URL. Roughly 25 minutes, most of it waiting.

**What this actually costs.** Cloud Run compute is **$0** — the always-free tier is 2M
requests, 180k vCPU-seconds and 360k GiB-seconds every month, forever, and this service
scales to zero when idle. Cloud Build is **$0** (2,500 free minutes/month; this build
uses ~6). The one line item that is not literally zero is **Artifact Registry storage**:
0.5 GB free, then $0.10/GB/month. The image is ~1.3 GB, so expect **≈ $0.08/month** once
the $300 signup credit expires after 90 days. Step 9 keeps that from growing.

A card is required to activate the account. It is not charged while you stay inside the
quotas, and step 3 sets a budget alarm before anything else happens.

---

## 0. Before you start

- A Google account
- A credit or debit card (verification only)
- Homebrew, which you already have

---

## 1. Install the CLI

```bash
brew install --cask google-cloud-sdk
```

Then open a new terminal (the installer edits your shell profile) and check:

```bash
gcloud version
```

---

## 2. Log in and create the project

```bash
gcloud auth login
```

A browser opens. Approve it.

```bash
# Project ids are globally unique — put something on the end if this is taken.
gcloud projects create lexora-prod --name="Lexora"
gcloud config set project lexora-prod
```

Now attach billing. Do this in the console once, it is not worth scripting:
<https://console.cloud.google.com/billing> → link `lexora-prod` to your billing account.
This is where the card and the $300 credit come in.

```bash
# Confirm it worked — billingEnabled must be true.
gcloud beta billing projects describe lexora-prod
```

---

## 3. Set a budget alarm FIRST

Before enabling anything that can bill. You will not be charged inside the free tier, but
an alarm means a mistake is an email rather than a surprise.

```bash
BILLING=$(gcloud beta billing accounts list --format="value(name)" | head -1)

gcloud billing budgets create \
  --billing-account="$BILLING" \
  --display-name="lexora-guardrail" \
  --budget-amount=5USD \
  --threshold-rule=percent=50 \
  --threshold-rule=percent=90 \
  --threshold-rule=percent=100
```

If that command is unavailable on your gcloud version, do it in the console:
<https://console.cloud.google.com/billing/budgets> → Create budget → $5 → alerts at
50/90/100%.

---

## 4. Enable the APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

Takes a minute or two.

---

## 5. Create the image repository

```bash
gcloud artifacts repositories create lexora \
  --repository-format=docker \
  --location=europe-west1 \
  --description="Lexora API images"
```

Any always-free region works. `europe-west1` is a reasonable default for Dubai latency;
`asia-south1` (Mumbai) is closer if you prefer.

---

## 6. Build the index locally

The image copies the portable index in rather than downloading two government websites at
deploy time. Build it first:

```bash
cd ~/lexora
make corpus
make index
```

You should see `contents cross-check  PASS` and `over window  0`.

---

## 7. Build the image — on Google's machines

```bash
gcloud builds submit --config infra/cloudbuild.yaml \
  --substitutions=_REGION=europe-west1,_REPO=lexora,_IMAGE=api
```

**Do not use a local `docker build` for this.** Your Mac is arm64 and Cloud Run runs
amd64; a local build produces an image that deploys cleanly and then dies with an
exec-format error. Cloud Build runs on amd64 natively and sidesteps the whole problem —
which is also why `docker buildx` not being installed on your machine does not matter.

Expect ~6 minutes. Most of it is downloading the two ONNX models, which are baked into
the image so a cold start is a model *load* rather than a *download*.

---

## 8. Deploy

```bash
gcloud run deploy lexora-api \
  --image europe-west1-docker.pkg.dev/lexora-prod/lexora/api:latest \
  --region europe-west1 \
  --allow-unauthenticated \
  --port 7860 \
  --memory 2Gi \
  --cpu 2 \
  --cpu-throttling \
  --min-instances 0 \
  --max-instances 3 \
  --concurrency 8 \
  --timeout 60s
```

The four flags that decide whether this stays free:

| Flag | Why |
|---|---|
| `--min-instances 0` | Scale to zero. An idle instance burns the GiB-second allowance for nothing |
| `--cpu-throttling` | Bill CPU only while a request is in flight |
| `--max-instances 3` | Caps worst case if the demo gets shared widely |
| `--memory 2Gi` | The cross-encoder needs headroom. Memory doubles GiB-second consumption, so this is the number to watch |

Grab the URL it prints:

```bash
gcloud run services describe lexora-api --region europe-west1 --format='value(status.url)'
```

---

## 9. Verify

```bash
URL=$(gcloud run services describe lexora-api --region europe-west1 --format='value(status.url)')

curl -s "$URL/api/health"                 # expect "status":"ok", 181 chunks
curl -s "$URL/api/laws" | head -c 300     # expect the four instruments

# The refusal path — the thing worth demoing
curl -s -X POST "$URL/api/ask" -H 'Content-Type: application/json' \
  -d '{"question":"What is the capital gains tax rate in Singapore?"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["kind"], "|", d["text"][:90])'
```

The first request after idle takes ~15–25 s while the container cold-starts and loads the
ONNX sessions. Subsequent ones are sub-second. **Warm it before a demo.**

---

## 10. Add the Claude key (once you have it)

```bash
echo -n "sk-ant-YOUR-KEY" | gcloud secrets create lexora-anthropic-key --data-file=-

PROJECT_NUMBER=$(gcloud projects describe lexora-prod --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding lexora-anthropic-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud run services update lexora-api --region europe-west1 \
  --set-secrets=LEXORA_ANTHROPIC_API_KEY=lexora-anthropic-key:latest
```

The key never appears in the image, in an environment variable you can `echo`, or in this
repository.

---

## 11. Point the web app at it, and lock CORS both ways

```bash
cd apps/web
npx vercel --prod
# set NEXT_PUBLIC_API_URL to the Cloud Run URL in the Vercel dashboard, then redeploy
```

Then allow that exact origin on the API — the allowlist is never `*`:

```bash
gcloud run services update lexora-api --region europe-west1 \
  --update-env-vars "LEXORA_CORS_ALLOW_ORIGINS=https://lexora.vercel.app"
```

---

## 12. Keep storage near zero

Every build adds an image version. Delete the old ones:

```bash
gcloud artifacts docker images list \
  europe-west1-docker.pkg.dev/lexora-prod/lexora/api \
  --include-tags --format='table(version,tags,createTime)'

gcloud artifacts docker images delete \
  europe-west1-docker.pkg.dev/lexora-prod/lexora/api@sha256:OLD_DIGEST --quiet
```

Or set it and forget it:

```bash
cat > /tmp/keep-3.json <<'EOF'
{"rules":[{"name":"keep-3","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":3}}]}
EOF
gcloud artifacts repositories set-cleanup-policies lexora \
  --location=europe-west1 --policy=/tmp/keep-3.json
```

---

## The same thing with Terraform

Everything above except the account setup is already declared in `infra/terraform/`, which
is what puts "Terraform (IaC)" on your résumé honestly:

```bash
cd infra/terraform
terraform init
terraform apply \
  -var project_id=lexora-prod \
  -var image=europe-west1-docker.pkg.dev/lexora-prod/lexora/api:latest \
  -var cors_allow_origins=https://lexora.vercel.app
```

It provisions the service, a dedicated service account (rather than the default compute
one), the Anthropic secret, and the same scaling limits.

---

## Tearing it down

```bash
gcloud run services delete lexora-api --region europe-west1
gcloud artifacts repositories delete lexora --location=europe-west1
# or remove the lot:
gcloud projects delete lexora-prod
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `exec format error` in logs | An arm64 image on an amd64 runtime | Rebuild with step 7, not local `docker build` |
| Container fails the startup probe | Cold start exceeded the deadline | `--timeout 60s` and check the log for the ONNX load line |
| `/api/health` says `degraded` | `var/index` missing from the image | Run `make index`, rebuild |
| Browser console shows a CORS error | Origin not on the allowlist | Step 11 — it must be the exact scheme+host |
| 429 from the API | Rate limiter, 10/min/IP | Expected. Raise `LEXORA_RATE_LIMIT` if you must |
| Build fails on `COPY var/index` | Index never built | Step 6 |
