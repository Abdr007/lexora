# Deploying Lexora to Hugging Face Spaces

> **Read this first.** An earlier version of this document said "no card, ever" and
> claimed 16 GB on the free tier. That was wrong on both counts and is corrected below.
> **Docker** Spaces — the kind this project needs, because it builds a Dockerfile —
> require a **PRO subscription ($9/month)**. Only *Static* Spaces remain free, and a
> static host cannot run a Python API.
>
> **This is now the live path.** PRO is subscribed and the Space is deployed at
> <https://huggingface.co/spaces/Abdr007/lexora>. Cloud Run remains supported and is
> cheaper in isolation, but one PRO subscription covers every Docker Space on the
> account, so the marginal cost of the second and third project is zero.

| | Spaces (Docker) | Cloud Run (always-free) |
|---|---|---|
| Card required | yes | yes, for verification |
| Cost | **$9/month** (PRO), any number of Spaces | **$0** compute + ~$0.08/mo image storage |
| RAM at runtime | 16 GB | 2 GB |
| RAM at **build** | lower, and undocumented — see below | same as runtime |
| Sleeps when idle | after 48 h | scales to zero immediately |
| Cold start | ~30 s | ~15–25 s |
| Résumé keyword | Docker, Hugging Face | **GCP, Cloud Run** |

Both clear the bar that matters at runtime: the service peaks at **524 MB** in a
container and is OOM-killed under 512 MB (AUDIT.md §6.4), which is what removed the free
512 MB tiers from the list. 2 GB is measured headroom, not a guess.

**The build container is a separate limit, and it is the one that bites.** Spaces gives
the *running* Space 16 GB but builds the image somewhere tighter. The first deploy here
was OOM-killed at build time (exit 137) even though the service fits comfortably in 1 GB
at runtime — see AUDIT.md §6.5. The bake step is therefore split across three `RUN`
layers so peak RSS is the largest stage rather than the sum of all three; `scripts/bake.py`
prints each stage's peak so the margin is visible in the build log rather than inferred
from a silent kill.

The same image runs on either host — the entrypoint honours `$PORT` when one is injected
and falls back to 7860 — so this is not a fork in the road.

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

If you would rather do it by hand, note that **`git push space HEAD:main` does not
work** and is not merely inconvenient. The Hub refuses binary files anywhere in a
pushed *history*, not just in the tip commit, so the screenshots committed to this
repository weeks ago reject today's push. Once the Space has been deployed once, a
plain push is also rejected as non-fast-forward, because what lives there is a single
synthetic commit rather than this project's history.

The script's equivalent is an orphan commit carrying the current tree minus `docs/`
(`SPACE_EXCLUDE`), with `.hf-space.yml` prepended to the README as frontmatter:

```bash
apps/api/.venv/bin/hf repo create lexora --repo-type space --space-sdk docker
git remote add space https://huggingface.co/spaces/YOUR_USERNAME/lexora
git checkout --orphan space-deploy
git rm -r --cached docs >/dev/null                # SPACE_EXCLUDE
# then prepend .hf-space.yml to README.md — the Space reads its config from there
git commit -q -m "Lexora — deployed tree"
git push --force space space-deploy:main
git checkout main && git branch -D space-deploy
```

Prefer the script. It also verifies the required artefacts are present *before*
pushing, so a missing index fails in a second rather than surfacing as a `degraded`
Space ten minutes later.

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

Live at **<https://uselexora.vercel.app>**, pointed at the Space.

**Set the environment variable before the first build, not after.** `next.config.ts`
derives the CSP's `connect-src` from `NEXT_PUBLIC_API_URL` at *build* time. Deploy without
it and the shipped policy pins `connect-src` to `http://127.0.0.1:7862`, so every request
from the browser is blocked by the page's own CSP — which looks exactly like an API
outage and is invisible in the API's logs, because no request ever leaves the browser.

```bash
cd apps/web
vercel link --yes --project lexora
printf 'https://YOUR_USERNAME-lexora.hf.space' | vercel env add NEXT_PUBLIC_API_URL production
vercel --prod --yes
```

Then allow that origin on the API. Space → Settings → Variables:

| Name | Value |
|---|---|
| `LEXORA_CORS_ALLOW_ORIGINS` | `https://uselexora.vercel.app` |

Read AUDIT.md §6.6 before trusting that setting: it is honoured on Cloud Run, but Spaces
attaches its own permissive CORS at the edge, so on *this* host the allowlist does not
constrain what a browser is allowed to do.

**Turn off Vercel deployment protection.** New projects default to Vercel Authentication,
which 302s every visitor to a Vercel login page — including the interviewer you sent the
link to. Project → Settings → Deployment Protection → Vercel Authentication → Disable.

Verify all four in one go, from the UI's own origin:

```bash
UI=https://uselexora.vercel.app
curl -sI "$UI/" | grep -i content-security-policy | tr ';' '\n' | grep connect-src  # names the Space
curl -s -o /dev/null -w '%{http_code}\n' "$UI/"                                     # 200, not 302
curl -s -D- -o /dev/null -H "Origin: $UI" .../api/health | grep -i allow-origin     # echoes the UI
curl -s -X POST .../api/ask -H 'Content-Type: application/json' -H "Origin: $UI" \
  -d '{"question":"What is the capital gains tax rate in Singapore?"}'              # "refusal"
```

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
