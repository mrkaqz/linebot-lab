# linebot-lab

A LINE bot for a group chat shared with an outside medical lab. The lab
posts photos of scanned blood-test (lab) results into the group; this bot
downloads each photo, transcribes it to Markdown, reads the patient's OPD
number out of the transcript, and uploads both the photo and the `.md`
transcript to a personal OneDrive, filed under a folder named for the OPD
number.

It ships with a small web admin UI (a separate port from the public
webhook -- see "Web admin UI" below) for configuring the bot, watching its
status, and fixing up results whose OPD number couldn't be read, so day-to-
day operation doesn't require SSH access to the Pi. A plain `.env` file
remains fully supported for anyone who'd rather configure it headlessly and
never open the UI at all.

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
- A way to expose this service's PUBLIC port (8000: `/line/webhook`,
  `/oauth/callback`) over HTTPS to the internet -- these instructions use
  [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
  (a free "quick tunnel"), but any HTTPS tunnel/reverse proxy works. Do
  **not** expose the admin port (8001) the same way -- see "Web admin UI"
  below for why -- LAN/local access to it is all you need
- Credentials for whichever `OCR_BACKEND` you choose: an Anthropic API key
  (`claude`), a Gemini API key (`gemini`), or nothing (`tesseract`, fully
  offline, and the default -- so this is the only one that needs nothing at
  all) -- or skip this until the admin UI is up and use its
  Setup > OCR > Test backend button instead

None of the above is needed just to **start the container** -- see "Setup"
below. They're needed to get lab results actually flowing end to end, and
every one of them is configurable from the admin UI once it's up.

## Setup

**Nothing is required to start the container.** Every setting -- LINE
credentials, the Microsoft/Entra block, the OCR backend and its key,
everything -- is configurable from the admin UI once it's up. The
walkthrough below is UI-first: start the container empty, then do the rest
of setup in the browser. Two ways to configure this bot, and you can mix
them freely:

- **The admin UI** (recommended, and the path below) -- open
  `http://<pi-address>:8001`, log in with the password printed to the logs
  on first boot, and use the Setup pages. Changes take effect immediately,
  no restart needed (with one exception, noted where it applies). See "Web
  admin UI" below.
- **`.env`, edited by hand** -- fully supported for anyone who'd rather
  configure headlessly over SSH and never open the UI at all. `.env.example`
  documents every field. Whatever isn't set in the UI falls back to `.env`;
  whatever isn't in either falls back to a sensible default where one
  exists -- see "Web admin UI" > "Config storage" below for the precedence.

### 1. Clone and start it empty

```bash
git clone <this repo>
cd linebot-lab
docker compose up --build
```

No `.env`, no environment variables, nothing filled in -- it boots anyway.
(Without Docker: `python3.12 -m venv venv && source venv/bin/activate &&
pip install -r requirements.txt && python -m app.main` -- note this runs
`python -m app.main`, not `uvicorn app.main:app`; the app starts two uvicorn
servers itself, public port 8000 and admin port 8001, from one process. See
"Web admin UI" below.)

### 2. Read the admin password from the logs, open the admin UI

Watch the startup logs for the **first-boot admin password**, inside an
unmissable banner:

```bash
docker compose logs | grep -A5 "FIRST BOOT"
```

Open `http://<pi-address>:8001` (or `localhost:8001` for local dev) and log
in with it. Change it immediately under Setup > General. The dashboard now
shows a setup checklist -- each remaining item links to the page that fixes
it; the rest of this walkthrough is that checklist in a sensible order.

### 3. Expose the service (cloudflared quick tunnel)

```bash
cloudflared tunnel --url http://localhost:8000
```

This prints a `https://<random>.trycloudflare.com` URL. You'll paste it
into the admin UI in the next step. Notice this only points at `:8000` --
there is deliberately no ingress rule anywhere pointing at `:8001` (the
admin UI); see "Web admin UI" below. A quick tunnel's hostname changes
every run -- for production, use a named Cloudflare Tunnel (or any other
stable HTTPS ingress) instead.

### 4. Entra ID app registration (for OneDrive)

Personal ("consumer") OneDrive only supports **delegated** auth -- there is
no app-only/client-credentials option -- so this app performs a one-time
interactive sign-in and stores the resulting refresh token.

1. In the [Entra admin center](https://entra.microsoft.com/), go to
   **Applications > App registrations > New registration**.
2. Set **Supported account types** to **Personal Microsoft accounts only**.
3. Under **API permissions**, add the delegated Microsoft Graph permission
   `Files.ReadWrite` (application code adds `offline_access` automatically).
4. Copy the **Application (client) ID** -- you'll paste it into the admin
   UI in the next step, along with the redirect URI it derives for you.

### 5. Setup > OneDrive: client ID, public base URL, and the derived URLs

In the admin UI, go to **Setup > OneDrive** and fill in:

- **Application (client) ID** from step 4.
- **Public base URL** -- the tunnel URL from step 3 (e.g.
  `https://abc123.trycloudflare.com`, no trailing slash).

Save. The page now displays two derived URLs, each with a **Copy** button --
this is the single most error-prone step of the whole setup, so copy them
exactly rather than retyping:

- **OAuth redirect URI** -- paste into the Entra app's **Authentication**
  section, as a platform of type **Mobile and desktop applications** (or
  "Web", either works for this flow) > **Redirect URIs**. The `secret=...`
  query string is part of the *exact* redirect URI you register -- this is
  intentional, it's the setup secret described below.
- **LINE webhook URL** -- you'll paste this into the LINE Developers console
  in step 6.

(Advanced: if the derived redirect URI isn't right for your setup, an
explicit override is available under "Advanced" on the same page -- see the
`MS_REDIRECT_URI` row in the configuration reference below.)

The **OAuth setup secret** embedded in that URL is generated automatically
on first boot -- you don't set it yourself. It exists so a stranger who
finds your public tunnel URL can't authorize the bot against *their own*
OneDrive account. It is never rotated automatically (rotating it would
break the Entra registration above until you update it to match) -- the
same page has an explicit **Regenerate** button, with a warning, for the
rare case you need to.

Saving this page rebuilds the OneDrive/MSAL client immediately -- no
restart needed.

### 6. LINE console

1. Create a Messaging API channel at the
   [LINE Developers console](https://developers.line.biz/console/).
2. Set the channel's **Webhook URL** to the LINE webhook URL you copied in
   step 5, and enable "Use webhook".
3. In the admin UI, go to **Setup > LINE** and paste in the channel's
   **Channel secret** and a long-lived **Channel access token**. Save.
4. Invite the bot to the LINE group the lab will post into, then click
   **Detect group** on the same page -- it puts the bot in listening mode
   for about 2 minutes; have someone post any message in the group and the
   group id (and its display name, if reachable) fills in automatically.
   This only *records* the id; nothing is filed until you click Save.
   (By hand instead: `LINE_LAB_GROUP_ID` is not shown anywhere in the LINE
   console -- with it unset the bot logs the group id of every message it
   sees, so post any message and read `groupId=...` out of the server
   logs.)

### 7. Choose an OCR backend

Easiest via the admin UI: **Setup > OCR**, pick a backend, paste its API key
(skip for `tesseract`, the default -- it needs nothing), and click **Test
backend** -- it runs the backend against a small bundled sample lab-report
image and shows you the extracted text before you trust it with a real
result. Saving hot-reloads the OCR converter immediately, no restart.

| Backend | Needs | Notes |
|---|---|---|
| `claude` | Anthropic API key, model (default `claude-opus-5`) | Official `anthropic` SDK, structured JSON output |
| `gemini` | Gemini API key, model (default `gemini-2.5-flash`) | `google-genai` SDK. List valid model ids for your key with `python -c "from google import genai; [print(m.name) for m in genai.Client(api_key='...').models.list()]"` |
| `tesseract` | nothing | Fully offline, Thai+English OCR via Tesseract; cannot answer "what is the OPD number" on its own -- `OPD_REGEX` is the only source of the OPD number with this backend |

Only the selected backend's credentials are needed -- a missing key for the
*other* backends is fine, and doesn't block startup either way (see
"Behaviour while unconfigured" below).

You can also try a backend against a sample photo from the command line,
without the admin UI or LINE wired up at all:

```bash
python scripts/ocr_check.py your-report.jpg --backend claude
python scripts/ocr_check.py your-report.jpg --compare   # try all three
```

Any jpg/png works; `app/static/sample_lab_result.jpg` is the bundled image
the UI's **Test backend** button uses, if you want something to try
immediately. No `.env` is needed -- the script runs against defaults and
only wants credentials for a backend you actually name.

### 8. First OneDrive authorization

**Setup > OneDrive > Sign in to OneDrive**, sign into the personal Microsoft
account that owns the target OneDrive, and approve the consent screen. Once
connected, use the **folder picker** on the same page to browse to (or
create) the folder lab results should file into, and click **Select this
folder** -- this is what `ONEDRIVE_ROOT` defaults to and remains a fallback
for if you skip the picker.

Approving consent lands you on `/oauth/callback` with `{"status": "ok",
...}`. The refresh token is now stored in `data/msal_cache.bin` (mode
`0600`) and every upload will use it via `acquire_token_silent` -- no
further manual steps. If it's ever revoked or expires, `/healthz` and the
admin dashboard both report it, and (if `ADMIN_LINE_ID` is set) the bot
pushes a LINE message asking you to repeat this step.

At this point the admin dashboard's setup checklist should be empty, and
the bot is fully live.

## Behaviour while unconfigured

Until every checklist item above is done, the bot runs safely in a
partially- or fully-unconfigured state rather than failing to boot:

- `/line/webhook` with no channel secret configured logs a warning and
  returns 200 without processing anything -- it never 500s, and it never
  skips signature verification once a secret *is* configured.
- `/healthz` always returns 200 (it's a liveness probe; the Docker
  healthcheck depends on that) but includes `"configured": false` and a
  `"missing"` object naming exactly what's left, grouped by LINE / OneDrive
  / OCR.
- An unconfigured or misconfigured OCR backend (e.g. `OCR_BACKEND=claude`
  with no key yet) doesn't crash the process or the background worker --
  the job that needed it fails cleanly to the unfiled queue, same as any
  other extraction failure.
- The admin dashboard shows the setup checklist prominently, with each item
  linking to the page that fixes it, until everything above is done.
- Startup logs one clear `WARNING` line per still-unconfigured capability
  (LINE / OneDrive / OCR), naming exactly what's missing.

## Web admin UI

The bot runs **two separate HTTP servers from one process** (see
`app/main.py`), not one app on one port:

- **Port 8000 -- public.** `/line/webhook`, `/oauth/callback`, `/oauth/start`,
  `/healthz`. This is the only port a tunnel (cloudflared or otherwise)
  should ever forward.
- **Port 8001 -- admin.** The whole admin UI: the dashboard, every Setup
  page, and the unfiled queue.

**Why two ports, and why not just "LAN only" IP filtering:** behind a
Cloudflare quick/named tunnel, `cloudflared` connects to the app over
`localhost`, so *every* request that arrives via the tunnel looks like it
came from `127.0.0.1` -- indistinguishable, by source IP, from a request
from someone sitting at the Pi's own console. IP-based "only allow LAN
requests" filtering cannot tell those apart, so it isn't used as the
security boundary here. Putting the admin UI on a second port that simply
never gets a tunnel ingress rule is: a request for it over the public
tunnel URL doesn't reach the container's admin routes at all.

### `SETUP_UI_EXPOSURE`

Controls whether the admin UI is *reachable* on the public port at all --
**login is required either way**, in both modes; this setting is about the
network path to the login page, not about skipping authentication.

- **`lan` (default).** The admin routes are mounted ONLY on the port-8001
  app. A request to any admin URL (`/`, `/setup/...`, `/unfiled`, `/login`,
  ...) on the public port-8000 app gets a plain 404 -- there's no route
  there to even reject with a login prompt. Publish port 8001 to your LAN
  (`docker-compose.yml` already does; just don't forward it through
  cloudflared) and reach it at `http://<pi-address>:8001`.
- **`public`.** The admin routes are ALSO mounted on the port-8000 app, for
  setups with no LAN access to the Pi (e.g. it's hosted somewhere you can
  only reach via the tunnel). Not the default, and not recommended unless
  you need it -- login/rate-limiting protect it either way, but exposing an
  admin login page to the internet at all is a larger attack surface than
  not doing so.

Which routes get mounted where is decided once, when the two FastAPI apps
are built at process startup -- Starlette doesn't support safely unmounting
routes from an app that's already serving traffic. **Changing
`SETUP_UI_EXPOSURE` (from `.env` or from Setup > General) requires a
container restart to take effect**; the UI tells you so when you change it.

### First login

On first boot, if no admin password has ever been set, one is generated
(`secrets.token_urlsafe`) and printed to the logs at `WARNING` inside an
unmissable banner:

```
docker compose logs | grep -A5 "FIRST BOOT"
```

It is never shown again after that -- if you lose it before changing it,
delete `data/linebot_lab.sqlite3`'s `config` table row (or simplest: stop
the container, remove `data/`, and reconfigure -- this also throws away the
OneDrive connection and unfiled queue, so prefer just re-reading the logs
from before you lost it if at all possible) and restart to generate a new
one. Log in at `http://<pi-address>:8001` and change it immediately under
Setup > General -- the password is hashed with stdlib `hashlib.scrypt` plus
a random per-password salt (no bcrypt/argon2 dependency added), verified
with `hmac.compare_digest`, and the session cookie is `httponly`,
`samesite=lax`, and additionally `secure`/HTTPS-only whenever
`SETUP_UI_EXPOSURE=public`. Five failed attempts from one IP lock out
further attempts from it for 15 minutes (logged at `WARNING`).

### Config storage: DB-backed, `.env` as fallback

Everything you set from the UI is written to a `config` table in the same
SQLite database as everything else (`data/linebot_lab.sqlite3`), not back
into `.env` -- **precedence is DB value (set via the UI) > environment
variable/`.env` > built-in default**, field by field. A field you've never
touched in the UI keeps reading from `.env`/the default exactly as before;
this is why an existing `.env`-only deployment keeps working untouched
after upgrading to a version with the admin UI.

Five fields are encrypted at rest before being written to that table --
`LINE_CHANNEL_SECRET`, `LINE_CHANNEL_ACCESS_TOKEN`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `OAUTH_SETUP_SECRET` -- using the `cryptography` package's Fernet, with a key
generated on first boot into `data/secret.key` (mode `0600`). **Be clear-
eyed about what this does and doesn't protect:** it protects a leaked or
copied *database file* -- a backup pulled off the Pi, a stolen SD card
image where the accompanying key file wasn't copied too, a bug that emails
the wrong attachment. It does **not** protect against anyone who has access
to the Pi's filesystem while both files are present (a shell in the
container, a full disk image, root on the host) -- the key sits in the same
place the ciphertext does, and they can read both equally easily. This is
local-key-at-rest encryption, not a secrets vault; the Pi itself, not this
encryption, is the actual trust boundary. The UI never renders a stored
secret back to you -- it shows "•••••••• (set)" or "not set" plus a
Replace field, and a separate Clear action; leaving a secret field blank on
save always means "leave it unchanged," never "clear it."

### Hot reload

Saving a config change rebuilds the affected runtime object(s) in place --
no restart needed for almost everything:

- Changing `OCR_BACKEND` (or its API key) re-instantiates MarkItDown and
  re-registers the OCR converter at priority -1, immediately. If the
  selected backend can't be built (e.g. no key yet), a placeholder is
  registered instead so this never crashes -- see "Behaviour while
  unconfigured" above.
- Changing the LINE channel access token rebuilds the LINE client
  immediately.
- Changing the Entra client ID, the public base URL, an explicit redirect
  URI override, or regenerating the OAuth setup secret (Setup > OneDrive)
  rebuilds the OneDrive/MSAL client immediately -- it re-reads the on-disk
  MSAL token cache fresh, so an existing sign-in survives unless the client
  ID itself changed to a different Entra app registration.
- Timezone, the OPD regex, the OneDrive filing root/folder, the group id,
  and the admin push-notification id all take effect on the very next
  event processed -- the background worker reads current settings fresh
  each time, not a value captured once at startup.
- The one exception is `SETUP_UI_EXPOSURE` (see above) -- which port(s) the
  admin UI is mounted on is fixed when the two FastAPI apps are built, and
  the UI tells you plainly that a restart is needed rather than silently
  doing nothing.

### OneDrive folder picker

Setup > OneDrive, once connected, offers a folder picker: browse the drive
(`GET /me/drive/root/children` and `.../items/{id}/children`, folders
only), with breadcrumb navigation and a "New folder" action. **Selecting a
folder stores both its OneDrive item id and its human-readable path** --
uploads address the folder by id (a rename in OneDrive afterwards doesn't
break filing; the current path is re-resolved from the id right before
each use), while the path is what's displayed in the UI. `ONEDRIVE_ROOT`
(a plain path string) remains a fully-working fallback for as long as no
folder has been picked.

### Unfiled queue

Setup: none needed -- `/unfiled` lists every result the bot couldn't match
to an OPD number, newest first, each with its OCR'd transcript, a preview
of the photo (proxied through the admin app with the bot's own OneDrive
token -- you're never handed a OneDrive sharing link, and your browser
never needs its own Microsoft sign-in), and a field for the correct OPD
number. Submitting **moves** both files in OneDrive (via
`PATCH /me/drive/items/{id}` with a new `parentReference`, not a
download/re-upload) from `_UNFILED/...` into the right `{opd}/` folder,
reusing the same sequence-suffix logic (`_2`, `_3`, ...) as normal filing
if a name collides there. If the move fails for any reason, the row is
left unresolved and the error is shown -- it is never marked resolved
optimistically. A **Dismiss** action exists for junk (an unrelated photo
someone posted in the group) that marks the row resolved without moving
anything.

## Running on Raspberry Pi

This is the intended deployment target: an always-on Raspberry Pi sitting
in the clinic, running the bot as a Docker container that survives reboots
and power cuts (`restart: unless-stopped` in `docker-compose.yml`).

**64-bit Raspberry Pi OS is required** -- see the note at the top of
"Requirements" above. Confirm with `uname -m` (must print `aarch64`)
before you start.

**Pull the prebuilt image (recommended -- no building on the Pi):**

GitHub Actions builds a multi-arch (`arm64` + `amd64`) image on every push
and publishes it to the GitHub Container Registry, so the Pi never has to
compile anything.

```bash
git clone <this repo>          # for docker-compose.yml and .env.example
cd linebot-lab
mkdir -p data
docker compose pull
docker compose up -d
```

No `.env` needed -- see "Setup" above. Copy `.env.example` to `.env` only if
you'd rather configure headlessly, or want to change the host ports (see
`PUBLIC_PORT`/`ADMIN_PORT` in the configuration reference below).

To update later: `docker compose pull && docker compose up -d`.

> **First pull failing with `denied` or `unauthorized`?** GHCR creates
> every new package **private**, even when the repository itself is public.
> Fix it once: on GitHub open the repo → **Packages** → `linebot-lab` →
> **Package settings** → **Change visibility** → **Public**. Alternatively,
> keep it private and authenticate on the Pi with a personal access token
> that has `read:packages`:
> ```bash
> echo <YOUR_PAT> | docker login ghcr.io -u <your-github-username> --password-stdin
> ```

`docker-compose.yml` pins `ghcr.io/mrkaqz/linebot-lab:${IMAGE_TAG:-latest}`.
`latest` is published from `main`; to run a specific build before merging,
set `IMAGE_TAG` in `.env` to the branch tag (slashes become dashes), e.g.
`IMAGE_TAG=claude-line-chatbot-lab-results-c5a1ck`. Git tags of the form
`v1.2.3` also publish `1.2.3` and `1.2` tags.

**Build locally instead** (only for development -- you do not need this on
the Pi):

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Either way, the container publishes both the public port (default 8000 --
`docker-compose.yml` is what your tunnel/cloudflared forwards) and the
admin UI port (default 8001). Reach the admin UI at
`http://<pi-address>:${ADMIN_PORT:-8001}` from any device on the same
LAN/Wi-Fi as the Pi -- a phone is fine, the UI is responsive down to phone
width, useful if you're setting the bot up from wherever the Pi physically
is rather than from a laptop with an SSH session open. **Do not** add a
cloudflared ingress rule (or any other public forward) for the admin port
-- see "Web admin UI" above for why. If you've remapped the default ports
(see `PUBLIC_PORT`/`ADMIN_PORT` in the configuration reference below),
substitute your values everywhere `8000`/`8001` appear in this README.

`data/` holds your OneDrive refresh token, the filing database (including
the admin UI's config table, activity log, and unfiled queue), and the
config-encryption/session-secret key files, so do **not** `chmod 777` it.
The container runs as uid 1000, and on Raspberry Pi OS the first user
account is also uid 1000 — so a `data/` directory you created yourself is
already owned correctly and needs no permission change at all. Only if
`docker compose logs` shows a permission error on `/app/data` do you need
to fix ownership explicitly:

```bash
sudo chown -R 1000:1000 data
```

`python:3.12-slim` is a multi-arch image manifest and the Dockerfile
hardcodes no `--platform`, so the same Dockerfile produces both the `arm64`
image the Pi runs and the `amd64` one you can run on a laptop.

### Deploying with Portainer

`portainer-stack.yml` in this repo is a ready-to-paste stack. In Portainer:
**Stacks → Add stack → Web editor**, paste it, and **Deploy** -- nothing
needs to be filled in. Portainer will still generate an *Environment
variables* section from the `${...}` placeholders in the file (currently
just `IMAGE_TAG`, `PUBLIC_PORT`, `ADMIN_PORT`); leave them at their defaults
unless you specifically need to pin an image tag or a port is already taken
on this host. Once it's running, read the first-boot admin password from
**Containers → linebot-lab → Logs**, and finish setup at
`http://<host>:${ADMIN_PORT:-8001}` exactly as in "Setup" above.

It differs from `docker-compose.yml` in two deliberate ways:

- **No `env_file`.** A Portainer stack has no `.env` beside it. Anyone who
  prefers headless/env-var config over the admin UI uses Portainer's own
  environment-variable editor instead -- see the commented-out block in
  `portainer-stack.yml` for the full list; uncomment and fill in only what
  you want to set that way.
- **A named volume, not a `./data` bind mount.** A relative bind path in a
  Portainer stack resolves against Portainer's internal stack directory on
  the host -- not where you'd expect, and awkward to back up. The named
  volume also fixes ownership for free: the image creates `/app/data` owned
  by uid 1000 before dropping to the non-root user, and Docker seeds a new
  named volume from the image path, so there's no `chown` step.

**`PUBLIC_PORT`/`ADMIN_PORT` are deploy-time only** -- Docker port mappings
resolved before the app process starts, not something the running app can
see or change. They are **not** in the admin UI; if you need to remap them,
do it in Portainer's Environment variables editor (or `docker-compose.yml`'s
`.env`) before/at deploy time, not by hunting for them in Setup.

Back up the `linebot-data` volume. It holds the OneDrive refresh token, the
filing database and the encryption keys; losing it means re-running the
OneDrive sign-in and reconfiguring.

### CI image builds

`.github/workflows/docker-publish.yml` runs the test suite first and
publishes only if it passes -- a broken build never reaches the registry.
It builds `linux/arm64` and `linux/amd64` under QEMU emulation, so the
first run takes roughly 15-25 minutes; the GitHub Actions layer cache makes
subsequent runs much faster. If your account has native ARM runners
available, switching to them is a large speed-up -- there's a comment in
the workflow marking exactly where.

Triggers: pushes to `main` and `claude/**` branches, `v*` tags, and manual
dispatch. Pull requests build without publishing.

**Speed expectations:** the `tesseract` backend runs entirely on the Pi's
CPU and takes a few seconds per photo on a Pi 4 -- fine for a clinic
receiving a handful of lab results a day, but noticeably slower than a
desktop. The `claude` and `gemini` backends just make an HTTPS call to a
cloud API, so the Pi's CPU is irrelevant to their speed -- they take about
as long on a Pi as anywhere else. For low daily volume, any of the three
backends is a reasonable choice; pick based on OCR quality and whether
you're comfortable sending report photos to a third-party API, not speed.


## Which number is the OPD number

The lab's reports print **three** id numbers in the header, and only one of
them is the filing key:

```
HN VET             : 00234654   <- the LAB's patient id   (8 digits)
LN VET             : 00350116   <- the LAB's specimen id  (8 digits)
HN Hospital/Clinic : 8258       <- the CLINIC's id (4 digits)  == the OPD number
```

`HN Hospital/Clinic` is the number *the referring clinic* assigned, so that is
what becomes the OneDrive folder name. `HN VET` and `LN VET` belong to the
laboratory; filing under either would scatter results under ids the clinic
does not use.

Two things make this easy to get wrong, and both are guarded:

- **The `OPD ` prefix is inconsistent.** Some reports print `HN
  Hospital/Clinic : OPD 9654`, others print a bare `HN Hospital/Clinic :
  8258`. So the prefix cannot be the anchor -- the *field label* is. The
  prefix is optional in the pattern and stripped from the captured value, so
  both spellings file as plain 4-digit folder names.
- **`HN` alone is ambiguous.** `HN VET` appears *before* `HN
  Hospital/Clinic` on every report, so a pattern anchored on a bare `HN`
  captures the laboratory's id instead. `OPD_REGEX` therefore matches the
  full `HN Hospital/Clinic` label, and captures exactly 4 digits so the
  lab's 8-digit ids cannot match even if OCR mangles the labels.

The same distinction is spelled out to the `claude`/`gemini` backends in
`app/ocr/prompt.py`. That matters because `resolve_opd` (`app/extract.py`)
**prefers the model's answer over the regex** when both find a number -- so
the prompt, not just the regex, has to name the right field.

A number that cannot be read as exactly 4 digits is left unset and the report
goes to the unfiled queue, where a human assigns it. That is deliberate: an
unfiled report is visible and fixable, whereas a *wrong* OPD number silently
files one patient's results under another's.
### What real OCR actually produces

Checked against both sample reports with Tesseract v5 (`lang=eng`), raw and
after the binarization `app/ocr/tesseract.py` applies. All four runs extract
the right number. Two things only real output revealed:

- **Tesseract reads the `l/` of `Hospital/Clinic` as `v`.** The real text
  contains `HN HospitalClinic` (slash dropped) and `HN HospitavClinic`. The
  pattern therefore matches the label loosely, as `Hospita.{0,4}Clinic`, and
  a stricter spelling silently misses.
- **It mangles the lab's own ids** -- `00234654` comes back as `90234654`,
  `00234769` as `0234769`. This is inert only because the capture is exactly
  4 digits, which those can never satisfy. It is a concrete reason not to
  loosen the length.

Both are pinned by fixtures in `tests/test_opd_regex.py` holding the verbatim
OCR strings, so a future change to the pattern cannot quietly regress them.

**One known gap.** Binarization split the second report's right-hand column
onto its own line, stranding the value two lines below its label; the
field-anchored branch cannot bridge that. That report survives only because
it happens to carry an `OPD ` prefix, which the generic fallback catches. The
same OCR damage on a report *without* the prefix yields no match, and the
report goes to the unfiled queue. That is the safe failure rather than a
misfile, but it is a miss --- `test_binarized_split_relies_on_opd_fallback`
documents it.


## When a photo produces an empty transcript

The single most confusing failure this app can have: a photo is picked up,
nothing errors, and it lands in the unfiled queue with a **blank** `.md` and
no OPD number.

The cause is almost always that **the OCR backend raised and MarkItDown
swallowed it.** MarkItDown catches a converter exception and moves on to the
next registered converter; its built-in `ImageConverter` then "succeeds" by
reading EXIF only, which produces no text. So a backend blowing up looks
exactly like a photo with nothing readable in it.

Things that trigger it:

- an expired, revoked, or mistyped API key for `claude` / `gemini`
- a rate limit, timeout, or 5xx from either API
- `OCR_BACKEND=tesseract` without the `tesseract-ocr-tha` language pack --
  the backend asks for `tha+eng`, and Tesseract hard-fails if either
  language is missing rather than falling back to the one it has

**The real exception is logged at ERROR by `app.ocr.base`**, with a full
traceback, immediately before it is swallowed. So the fix is always: read
the logs.

```bash
docker compose logs | grep -A20 "OCR backend .* failed"
```

`scripts/ocr_check.py` turns logging on for this reason -- running a photo
through it prints the underlying error rather than an unexplained empty
result:

```bash
python scripts/ocr_check.py your-report.jpg --backend tesseract
```

Note this is distinct from a backend that could not be *constructed* at all
(no API key set yet): that is caught at startup, logged as a WARNING, and
reported as a setup-checklist item -- see "Behaviour while unconfigured"
above.

## Configuration reference

**Nothing is required.** Every field below has a working default or is
allowed to stay unset; the container boots either way, and an unfinished
capability (LINE / OneDrive / OCR) is reported by the setup checklist and
`/healthz` rather than blocking startup -- see "Behaviour while
unconfigured" above. "UI-settable" means: also settable/changeable from the
admin UI (Setup pages), where it takes precedence over the `.env` value
below -- see "Web admin UI" > "Config storage" above.

| Variable | Required | Default | UI-settable | Description |
|---|---|---|---|---|
| `LINE_CHANNEL_SECRET` | no | unset | yes (Setup > LINE) | LINE channel secret, verifies webhook signatures. Encrypted at rest when set via the UI. Until set, `/line/webhook` logs a warning and returns 200 without processing anything. |
| `LINE_CHANNEL_ACCESS_TOKEN` | no | unset | yes (Setup > LINE) | LINE channel access token, calls the Messaging API. Encrypted at rest when set via the UI; hot-reloads the LINE client. |
| `LINE_LAB_GROUP_ID` | no | unset | yes (Setup > LINE, incl. "Detect group") | The LINE group to process photos from; unset = process nothing |
| `ADMIN_LINE_ID` | no | unset | yes (Setup > LINE) | LINE user id for admin push notifications (unfiled results, OneDrive re-auth); unset = skip silently |
| `OCR_BACKEND` | no | `tesseract` | yes (Setup > OCR) | `claude` \| `gemini` \| `tesseract`. Hot-reloads MarkItDown's registered converter. `tesseract` needs no credentials -- it's the only backend the container can use out of the box. |
| `CLAUDE_MODEL` | no | `claude-opus-5` | yes (Setup > OCR) | Anthropic model id (only used if `OCR_BACKEND=claude`) |
| `ANTHROPIC_API_KEY` | no (needed if `OCR_BACKEND=claude`) | unset | yes (Setup > OCR) | Anthropic API key. Encrypted at rest when set via the UI. Missing while selected -- OCR jobs fail cleanly to the unfiled queue rather than crashing; reported by the setup checklist. |
| `GEMINI_MODEL` | no | `gemini-2.5-flash` | yes (Setup > OCR) | Gemini model id (only used if `OCR_BACKEND=gemini`) -- list valid ids with `client.models.list()` |
| `GEMINI_API_KEY` | no (needed if `OCR_BACKEND=gemini`) | unset | yes (Setup > OCR) | Google Gemini API key. Encrypted at rest when set via the UI. Same fail-clean behaviour as `ANTHROPIC_API_KEY` above. |
| `OPD_REGEX` | no | see `.env.example` | yes (Setup > General, with a "test regex" box) | Regex used to find/cross-check the OPD number in the transcript. Anchored on the report's `HN Hospital/Clinic` field and captures exactly 4 digits -- see [Which number is the OPD number](#which-number-is-the-opd-number) before loosening it |
| `TIMEZONE` | no | `Asia/Bangkok` | yes (Setup > General) | IANA timezone used to compute the received date from the LINE event timestamp |
| `ONEDRIVE_ROOT` | no | `/LabResults` | indirectly -- see `onedrive_folder_id`/`_path` below | OneDrive root folder (a path string) under which per-OPD subfolders are created; the fallback for as long as no folder has been picked in the UI |
| *(no `.env` var -- UI only)* | no | unset | Setup > OneDrive folder picker | `onedrive_folder_id` / `onedrive_folder_path`: the OneDrive item id (used for addressing -- survives a rename) and human-readable path (display only) of the picked filing folder. Takes precedence over `ONEDRIVE_ROOT` once set. |
| `MS_CLIENT_ID` | no | unset | yes (Setup > OneDrive) | Entra app registration client id. Hot-reloads the OneDrive/MSAL client. |
| `PUBLIC_BASE_URL` | no | unset | yes (Setup > OneDrive) | This service's public HTTPS base URL, no trailing slash. Derives both the OAuth redirect URI and the LINE webhook URL -- both are displayed with copy buttons on Setup > OneDrive. Hot-reloads the OneDrive/MSAL client. |
| `MS_REDIRECT_URI` | no | unset | yes (Setup > OneDrive, "Advanced") | Explicit override of the derived OAuth redirect URI; wins over `PUBLIC_BASE_URL` when set. Must exactly match a redirect URI on the Entra app, including `?secret=<OAUTH_SETUP_SECRET>`. Hot-reloads the OneDrive/MSAL client. |
| `OAUTH_SETUP_SECRET` | no | auto-generated on first boot | yes (Setup > OneDrive, "Regenerate") | Shared secret required on `/oauth/start` and `/oauth/callback` (the admin UI's "Sign in to OneDrive" button appends it for you). Encrypted at rest when stored via the DB. Never rotated automatically -- see Setup > OneDrive above for why. Hot-reloads the OneDrive/MSAL client. |
| `DATA_DIR` | no | `data` | no | Directory for the SQLite database (also the admin UI's config table, activity log, and key files) and the MSAL token cache |
| `LOG_LEVEL` | no | `INFO` | no | Python logging level |
| `SETUP_UI_EXPOSURE` | no | `lan` | yes (Setup > General) | `lan` \| `public` -- see "Web admin UI" above. Changing it needs a container restart either way it's set. |
| `PUBLIC_PORT` | no | `8000` | **no -- deploy-time only** | Docker's HOST port for the public app (container port stays 8000). Resolved by Docker before the app starts; not a running-app setting, so it's not in the admin UI. See `docker-compose.yml`/`portainer-stack.yml`. |
| `ADMIN_PORT` | no | `8001` | **no -- deploy-time only** | Docker's HOST port for the admin UI (container port stays 8001). Same deploy-time caveat as `PUBLIC_PORT` above. |

The admin password itself has no `.env` variable at all -- it always lives
only in the DB (hashed), generated randomly on first boot if never set. See
"Web admin UI" > "First login".

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

### On Windows

The project targets Python 3.12 but runs fine on 3.13; every pinned
dependency publishes cp313 wheels. Two things differ:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install tzdata
.venv\Scripts\python.exe -m pytest -q
```

- The interpreter lives at `.venv\Scripts\python.exe`, not `.venv/bin/python`.
- **`tzdata` is required.** Windows ships no system tz database, so
  `zoneinfo` raises `ZoneInfoNotFoundError` for every key including `UTC`,
  and nine tests fail -- all of `test_timezone.py` plus the idempotency and
  boot tests that file by date. It is not in `requirements.txt` because the
  Linux container gets its tz data from the base image; install it into the
  venv by hand. If a fresh checkout shows timezone failures, this is why.

To exercise the `tesseract` backend locally you also need the Tesseract
binary on `PATH` *and* both `eng` and `tha` traineddata -- the backend asks
for `tha+eng` and Tesseract fails outright if either is missing (see "When
a photo produces an empty transcript" above). The Docker image installs
both; a stock Windows Tesseract install usually has only `eng`.

Tests run fully offline with no API keys set (and no environment variables
set at all, for the zero-config-boot tests) -- all network calls (LINE,
Microsoft Graph, Anthropic, Gemini) are mocked or simply never exercised by
the unit-tested pure logic (signature verification, the OPD regex,
filename sequencing, the timezone boundary, idempotency, MarkItDown
converter priority, and the ERROR log a failing OCR backend must emit before
MarkItDown swallows it), plus the admin UI's config precedence, secret
encryption, password hashing, per-route auth enforcement, `lan`/`public`
route mounting, the unfiled-queue move-or-leave-unresolved logic, group
auto-detect, and booting/serving/hot-reloading with zero required
environment variables (`tests/test_unconfigured_boot.py`).

## Project layout

```
linebot-lab/
├── app/
│   ├── main.py            # entrypoint: builds public_app (8000) + admin_app (8001),
│   │                       #   one shared AppState, runs both uvicorn servers
│   ├── runtime.py          # AppState: the shared store/queue/clients/settings + hot reload
│   ├── config.py           # pydantic-settings Settings + settings_from_overrides()
│   ├── settings_store.py   # DB-backed config table, DB > env > default, secret encryption
│   ├── crypto.py            # Fernet secret encryption + scrypt password hashing
│   ├── auth.py              # admin session/login, rate limiting
│   ├── admin/
│   │   └── router.py        # the whole admin UI: dashboard, Setup pages, unfiled queue
│   ├── templates/            # Jinja2 templates for the admin UI (server-rendered, no build step)
│   ├── static/                # style.css, admin.js, and the OCR "Test backend" sample image
│   ├── line_client.py       # signature verify, content download, reply/push, group summary
│   ├── pipeline.py          # per-image job: download -> extract -> path -> upload
│   ├── extract.py           # MarkItDown wiring + OPD regex resolution
│   ├── onedrive.py           # MSAL delegated auth + Microsoft Graph upload/move/browse
│   ├── store.py              # SQLite: processed message ids, unfiled log, activity log
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
