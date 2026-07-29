# Deploying Lexora to Hugging Face Spaces

> **Read this first.** An earlier version of this document said "no card, ever" and
> claimed 16 GB on the free tier. That was wrong on both counts and is corrected below.
> **Docker** Spaces — the kind this project needs, because it builds a Dockerfile —
> require a **PRO subscription ($9/month)**. Only *Static* Spaces remain free, and a
> static host cannot run a Python API. Cloud Run is the cheaper path: its always-free
> allowance covers this workload at no cost.

| | Spaces (Docker) | Cloud Run (always-free) |
|---|---|---|
| Card required | yes | yes, for verification |
| Cost | **$9/month** (PRO) | **$0** compute + ~$0.08/mo image storage |
| RAM | 16 GB | 2 GB |
| Sleeps when idle | after 48 h | scales to zero immediately |
| Cold start | ~30 s | ~15–25 s |
| Résumé keyword | Docker, Hugging Face | **GCP, Cloud Run** |

Both clear the bar that matters: the service peaks at **524 MB** in a container and is
OOM-killed under 512 MB (AUDIT.md §6.4), which is what removed the free 512 MB tiers from
the list. 2 GB is measured headroom, not a guess.

The same image runs on either — the entrypoint honours `$PORT` when one is injected and
falls back to 7860 — so this is not a fork in the road. Use
[DEPLOY-CLOUDRUN.md](DEPLOY-CLOUDRUN.md) unless you already pay for PRO.

---

## 1. Create a write token

<https://huggingface.co/settings/tokens> → **Create new token** → type **Write** → copy it.

Store it locally. Do this in *your* terminal so the token is never pasted into a chat
transcript:

```bash
cd ~/lexora
apps/api/.venv/bin/hf auth login --token hf_YOUR_TOKEN_HERE --add-to-git-credential
```

`--add-to-git-credential` matters: the deploy pushes over https, and without it git
prompts for a password the Hub no longer accepts.

(`huggingface-cli` still appears in a lot of documentation. It is deprecated as of
`huggingface_hub` 1.x and now refuses to run — `hf` is the replacement.)

Verify:

```bash
apps/api/.venv/bin/python -c "from huggingface_hub import HfApi; print(HfApi().whoami()['name'])"
```

---

## 2. Build the index

The image copies the portable index in rather than downloading two government websites at
deploy time.

```bash
make corpus
make index
```

Expect `contents cross-check  PASS` and `over window  0`.

---

## 3. Create the Space and push

```bash
apps/api/.venv/bin/python scripts/deploy_space.py --space lexora --public
```

That script is idempotent: it creates the Space if it does not exist, commits any
outstanding work locally, adds the Space as a git remote, and pushes. Re-run it to
redeploy.

If you would rather do it by hand:

```bash
apps/api/.venv/bin/hf repo create lexora --repo-type space --space-sdk docker
git remote add space https://huggingface.co/spaces/YOUR_USERNAME/lexora
git add -A && git commit -m "Lexora v1.0.0"
git push space HEAD:main
```

---

## 4. What the Space does with the repo

`README.md` carries the YAML frontmatter Spaces reads as configuration:

```yaml
sdk: docker
app_port: 7860
```

So it builds the root `Dockerfile` and routes traffic to 7860 — which is exactly what the
container already listens on. Nothing Spaces-specific exists anywhere else in the code.

The build takes **8–12 minutes**, almost all of it downloading the two ONNX models, which
are baked into the image so a cold start is a model *load* rather than a *download*.
Watch it under the **Logs** tab.

---

## 5. Add the Claude key

Space → **Settings** → **Variables and secrets** → **New secret**:

| Name | Value |
|---|---|
| `LEXORA_ANTHROPIC_API_KEY` | `sk-ant-…` |

The Space restarts. The header badge flips from amber `Offline · extractive` to
`Claude · grounded`. The key is never in the image, in the repo, or in a build log.

---

## 6. Verify

```bash
SPACE=https://YOUR_USERNAME-lexora.hf.space

curl -s "$SPACE/api/health"              # "status":"ok", 181 chunks
curl -s "$SPACE/api/laws" | head -c 200  # the four instruments

curl -s -X POST "$SPACE/api/ask" -H 'Content-Type: application/json' \
  -d '{"question":"What is the capital gains tax rate in Singapore?"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["kind"], "|", d["text"][:80])'
```

The last one must print `refusal`. That is the demo.

---

## 7. Point the web app at it

```bash
cd apps/web && npx vercel --prod
```

Set `NEXT_PUBLIC_API_URL` to `https://YOUR_USERNAME-lexora.hf.space` in the Vercel
dashboard, redeploy, then allow that origin on the API — the allowlist is never `*`.
Space → Settings → Variables:

| Name | Value |
|---|---|
| `LEXORA_CORS_ALLOW_ORIGINS` | `https://lexora.vercel.app` |

---

## 8. Before a demo, warm it

A Space sleeps after 48 hours idle and takes ~30 s to wake, plus the ONNX load.

```bash
curl -s https://YOUR_USERNAME-lexora.hf.space/api/health >/dev/null
```

Run that a minute before you present. Or keep the tab open.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build fails on `COPY var/index` | Index never built, or `var/index` still gitignored | `make index`, confirm `git status` shows the files tracked |
| Space stuck on "Building" | Model download is slow | Normal for the first build; 8–12 min |
| `/api/health` says `degraded` | Index missing from the image | Check the build log for the `COPY var/index` step |
| CORS error in the browser | Origin not on the allowlist | §7 — exact scheme + host |
| 429 from the API | Rate limiter, 30/min/IP | Expected. Raise `LEXORA_RATE_LIMIT` in Space settings if needed |
| Push rejected | Token is read-only | Create a **Write** token in §1 |
