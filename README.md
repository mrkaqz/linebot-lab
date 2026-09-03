# linebot-lab

A LINE bot for a group chat shared with an outside medical lab. The lab
posts photos of scanned blood-test (lab) results into the group; this bot
downloads each photo, transcribes it to Markdown, reads the patient's OPD
number out of the transcript, and uploads both the photo and the `.md`
transcript to a personal OneDrive, filed under a folder named for the OPD
number.

```
photo posted in LINE group
        │
        ▼
  download image (LINE Messaging API)
        │
        ▼
  MarkItDown.convert()  ──▶  OCR backend (claude | gemini | tesseract)
        │                         transcribes the report to Markdown,
        │                         extracts opd_number / patient_name
        ▼
  OPD_REGEX cross-check / fallback
        │
        ▼
  {ONEDRIVE_ROOT}/{opd}/{received-date}.jpg + .md
  (or {ONEDRIVE_ROOT}/_UNFILED/{date}/ if no OPD number was found)
```

**Important:** MarkItDown does not OCR images out of the box -- its
built-in image converter only extracts EXIF metadata. The OCR/transcription
step is a custom `DocumentConverter` (in `app/ocr/`) registered with
MarkItDown at **priority -1**, so it is tried *before* the built-in
converter. See the docstring in `app/extract.py` for why this matters.

## Requirements

> **Deployed target: Raspberry Pi, 64-bit OS only.** This service is
> designed to run as an always-on appliance on a Raspberry Pi (Pi 4, Pi 5,
> or a Pi 3 / Pi Zero 2 W running the 64-bit image), with 2 GB RAM or more
> recommended -- MarkItDown's `magika` file-type detector loads an
> `onnxruntime` model at import time regardless of which `OCR_BACKEND` you
> pick. **32-bit Raspberry Pi OS (armv7) will not work**: neither
> `onnxruntime` nor `pillow` publish 32-bit ARM wheels, and MarkItDown hard-
> depends on `magika`, which hard-depends on `onnxruntime` -- there is no
> way to `pip install` this on 32-bit Pi OS short of compiling ONNX Runtime
> from source (hours, and it usually fails on Pi hardware anyway). Check
> before you start:
> ```bash
> uname -m   # must print aarch64 -- if it prints armv7l, reflash the Pi
>            # with the 64-bit Raspberry Pi OS image first
> ```
> See "Running on Raspberry Pi" below for build instructions.

- Python 3.12 (the Dockerfile uses `python:3.12-slim`)
- `tesseract-ocr` + `tesseract-ocr-tha` system packages, only if you use
  `OCR_BACKEND=tesseract` (already installed in the Docker image)
- A LINE Messaging API channel
- An Entra ID (Azure AD) app registration for personal Microsoft accounts
  (for OneDrive)
- A way to expose this service's `/line/webhook` and `/oauth/callback`
  routes over HTTPS to the internet -- these instructions use
  [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
  (a free "quick tunnel"), but any HTTPS tunnel/reverse proxy works
- Credentials for whichever `OCR_BACKEND` you choose: an Anthropic API key
  (`claude`), a Gemini API key (`gemini`), or nothing (`tesseract`, fully
  offline)

## Setup

### 1. Clone and configure

```bash
git clone <this repo>
cd linebot-lab
cp .env.example .env
```

You'll fill in `.env` as you go through the steps below. **Never commit
`.env`** -- this is a public repo (`.env` is already in `.gitignore`).

### 2. LINE console

1. Create a Messaging API channel at the
   [LINE Developers console](https://developers.line.biz/console/).
2. Copy the **Channel secret** into `LINE_CHANNEL_SECRET`.
3. Issue a long-lived **Channel access token** and copy it into
   `LINE_CHANNEL_ACCESS_TOKEN`.
4. Invite the bot to the LINE group the lab will post into. Leave
   `LINE_LAB_GROUP_ID` blank for now -- see step 9.
5. Set the channel's webhook URL to `https://<your-tunnel-host>/line/webhook`
   (see step 4, cloudflared, below) and enable "Use webhook".

### 3. Entra ID app registration (for OneDrive)

Personal ("consumer") OneDrive only supports **delegated** auth -- there is
no app-only/client-credentials option -- so this app performs a one-time
interactive sign-in and stores the resulting refresh token.

1. In the [Entra admin center](https://entra.microsoft.com/), go to
   **Applications > App registrations > New registration**.
2. Set **Supported account types** to **Personal Microsoft accounts only**.
3. Under **Authentication**, add a platform of type **Mobile and desktop
   applications** (or "Web", either works for this flow) with a redirect
   URI matching what you'll set as `MS_REDIRECT_URI` below, e.g.
   `https://<your-tunnel-host>/oauth/callback?secret=<OAUTH_SETUP_SECRET>`.
   The query string with `secret=...` must be part of the *exact* redirect
   URI you register -- this is intentional, see step 6.
4. Under **API permissions**, add the delegated Microsoft Graph permission
   `Files.ReadWrite` (application code adds `offline_access` automatically).
5. Copy the **Application (client) ID** into `MS_CLIENT_ID`.

### 4. Expose the service (cloudflared quick tunnel)

```bash
cloudflared tunnel --url http://localhost:8000
```

This prints a `https://<random>.trycloudflare.com` URL. Use it (or a
stable tunnel hostname, if you set one up) as `<your-tunnel-host>` in the
LINE webhook URL, `MS_REDIRECT_URI`, and when visiting `/oauth/start`
below. A quick tunnel's hostname changes every run -- for production, use a
named Cloudflare Tunnel (or any other stable HTTPS ingress) instead.

### 5. Choose an OCR backend

Set `OCR_BACKEND` in `.env` to one of:

| Backend | Needs | Notes |
|---|---|---|
| `claude` | `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` (default `claude-opus-5`) | Official `anthropic` SDK, structured JSON output |
| `gemini` | `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash`) | `google-genai` SDK. List valid model ids for your key with `python -c "from google import genai; [print(m.name) for m in genai.Client(api_key='...').models.list()]"` |
| `tesseract` | nothing | Fully offline, Thai+English OCR via Tesseract; cannot answer "what is the OPD number" on its own -- `OPD_REGEX` is the only source of the OPD number with this backend |

Only the selected backend's credentials are required -- a missing key for
the *other* backends is fine. Startup fails loudly if the *selected*
backend is missing its credentials.

You can try a backend against a sample photo before wiring up LINE at all:

```bash
python scripts/ocr_check.py sample-lab-report.jpg --backend claude
python scripts/ocr_check.py sample-lab-report.jpg --compare   # try all three
```

### 6. Generate the OAuth setup secret

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `OAUTH_SETUP_SECRET`, and make sure the *exact same* value
appears in the `secret=` query param of `MS_REDIRECT_URI` (both in `.env`
and in the Entra app registration's redirect URI). `/oauth/start` and
`/oauth/callback` both require this secret as a query param -- without it,
anyone who finds your tunnel URL while testing could authorize the bot
against *their own* OneDrive account instead of yours.

### 7. Run it

```bash
docker compose up --build
```

or, without Docker:

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### 8. First OneDrive authorization

With the service and tunnel both running, visit, in a browser signed into
the personal Microsoft account that owns the target OneDrive:

```
https://<your-tunnel-host>/oauth/start?secret=<OAUTH_SETUP_SECRET>
```

Approve the consent screen. You should land on `/oauth/callback` with
`{"status": "ok", ...}`. The refresh token is now stored in
`data/msal_cache.bin` (mode `0600`) and every upload will use it via
`acquire_token_silent` -- no further manual steps. If it's ever revoked or
expires, `/healthz` reports unhealthy and (if `ADMIN_LINE_ID` is set) the
bot pushes a LINE message asking you to repeat this step.

### 9. Find the LINE group id

`LINE_LAB_GROUP_ID` is not shown anywhere in the LINE console. With
`LINE_LAB_GROUP_ID` left blank, the bot logs the group id of every message
it sees (and processes nothing else, fail-safe) -- have someone post any
message in the group, find the `groupId=...` in the server logs, put it in
`.env`, and restart the service.

## Running on Raspberry Pi

This is the intended deployment target: an always-on Raspberry Pi sitting
in the clinic, running the bot as a Docker container that survives reboots
and power cuts (`restart: unless-stopped` in `docker-compose.yml`).

**64-bit Raspberry Pi OS is required** -- see the note at the top of
"Requirements" above. Confirm with `uname -m` (must print `aarch64`)
before building.

**Build directly on the Pi (simplest, recommended):**

```bash
git clone <this repo>
cd linebot-lab
cp .env.example .env   # fill in as in Setup above
mkdir -p data
docker compose up -d --build
```

`data/` holds your OneDrive refresh token and the filing database, so do
**not** `chmod 777` it. The container runs as uid 1000, and on Raspberry Pi
OS the first user account is also uid 1000 — so a `data/` directory you
created yourself is already owned correctly and needs no permission change
at all. Only if `docker compose logs` shows a permission error on
`/app/data` do you need to fix ownership explicitly:

```bash
sudo chown -R 1000:1000 data
```

`python:3.12-slim` is a multi-arch image manifest, and the Dockerfile
hardcodes no `--platform`, so building on the Pi itself simply pulls and
builds the `arm64` variant -- no cross-compilation flags needed.

**Cross-build from an x86 machine instead**, if you'd rather not build on
the Pi's own CPU (e.g. to push to a registry the Pi then pulls from):

```bash
docker buildx build --platform linux/arm64 -t linebot-lab .
```

**Speed expectations:** the `tesseract` backend runs entirely on the Pi's
CPU and takes a few seconds per photo on a Pi 4 -- fine for a clinic
receiving a handful of lab results a day, but noticeably slower than a
desktop. The `claude` and `gemini` backends just make an HTTPS call to a
cloud API, so the Pi's CPU is irrelevant to their speed -- they take about
as long on a Pi as anywhere else. For low daily volume, any of the three
backends is a reasonable choice; pick based on OCR quality and whether
you're comfortable sending report photos to a third-party API, not speed.

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `LINE_CHANNEL_SECRET` | yes | -- | LINE channel secret, verifies webhook signatures |
| `LINE_CHANNEL_ACCESS_TOKEN` | yes | -- | LINE channel access token, calls the Messaging API |
| `LINE_LAB_GROUP_ID` | no | unset | The LINE group to process photos from; unset = process nothing |
| `ADMIN_LINE_ID` | no | unset | LINE user id for admin push notifications (unfiled results, OneDrive re-auth); unset = skip silently |
| `OCR_BACKEND` | no | `tesseract` | `claude` \| `gemini` \| `tesseract` |
| `CLAUDE_MODEL` | no | `claude-opus-5` | Anthropic model id (only used if `OCR_BACKEND=claude`) |
| `ANTHROPIC_API_KEY` | if `OCR_BACKEND=claude` | unset | Anthropic API key |
| `GEMINI_MODEL` | no | `gemini-2.5-flash` | Gemini model id (only used if `OCR_BACKEND=gemini`) -- list valid ids with `client.models.list()` |
| `GEMINI_API_KEY` | if `OCR_BACKEND=gemini` | unset | Google Gemini API key |
| `OPD_REGEX` | no | see `.env.example` | Regex used to find/cross-check the OPD number in the transcript |
| `TIMEZONE` | no | `Asia/Bangkok` | IANA timezone used to compute the received date from the LINE event timestamp |
| `ONEDRIVE_ROOT` | no | `/LabResults` | OneDrive root folder under which per-OPD subfolders are created |
| `MS_CLIENT_ID` | yes | -- | Entra app registration client id |
| `MS_REDIRECT_URI` | yes | -- | Must exactly match a redirect URI on the Entra app; include `?secret=<OAUTH_SETUP_SECRET>` |
| `OAUTH_SETUP_SECRET` | yes | -- | Shared secret required on `/oauth/start` and `/oauth/callback` |
| `DATA_DIR` | no | `data` | Directory for the SQLite database and the MSAL token cache |
| `LOG_LEVEL` | no | `INFO` | Python logging level |

## Filing behaviour

- Destination date comes from the LINE event's `timestamp` (epoch
  milliseconds, UTC), converted to `TIMEZONE` -- **not** server local time,
  **not** `datetime.now()`.
- `{ONEDRIVE_ROOT}/{opd}/{date}.jpg` + `{date}.md` when an OPD number was
  found; a second result for the same OPD on the same day becomes
  `{date}_2.jpg` / `{date}_2.md`, then `_3`, etc. -- the photo and its `.md`
  always share the same stem.
- `{ONEDRIVE_ROOT}/_UNFILED/{date}/{date}.jpg` + `.md` when no OPD number
  could be determined. Nothing is ever dropped; unfiled results are also
  logged to a local `unfiled` SQLite table, and pushed to `ADMIN_LINE_ID`
  if configured.
- LINE webhook retries are de-duplicated by `messageId` in a local SQLite
  `processed` table, checked *before* any download/OCR/upload work starts.

## Development / tests

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pytest
```

Tests run fully offline with no API keys set -- all network calls (LINE,
Microsoft Graph, Anthropic, Gemini) are mocked or simply never exercised by
the unit-tested pure logic (signature verification, the OPD regex,
filename sequencing, the timezone boundary, idempotency, and MarkItDown
converter priority).

## Project layout

```
linebot-lab/
├── app/
│   ├── main.py          # FastAPI: /line/webhook, /oauth/start, /oauth/callback, /healthz
│   ├── config.py         # pydantic-settings
│   ├── line_client.py    # signature verify, content download, reply/push
│   ├── pipeline.py       # per-image job: download -> extract -> path -> upload
│   ├── extract.py        # MarkItDown wiring + OPD regex resolution
│   ├── onedrive.py        # MSAL delegated auth + Microsoft Graph upload
│   ├── store.py           # SQLite: processed message ids, unfiled log
│   └── ocr/
│       ├── base.py        # shared DocumentConverter base + LabResult
│       ├── prompt.py       # one prompt + one JSON schema, shared by claude.py & gemini.py
│       ├── claude.py       # Anthropic backend
│       ├── gemini.py       # Google Gemini backend
│       └── tesseract.py    # local offline backend
├── scripts/
│   ├── ocr_check.py       # run just the extractor on a local jpg
│   └── replay.py          # replay a saved webhook payload
└── tests/
```

## License

AGPL-3.0 -- see `LICENSE`.
