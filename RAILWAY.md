# Hosting the HRP reports on Railway

End state: one always-on service that syncs Zoho and sends all three reports on
schedule, with no dependence on a Windows machine being awake. Windows Task
Scheduler goes away entirely.

Allow about two hours end to end, most of it waiting on deploys.

---

## Before you start

- A GitHub account, and `git` installed.
- A Railway account on the **Hobby plan ($5/month)**. Free/Trial cannot mount a
  volume large enough to be useful and has tighter limits.
- Your existing `config.py` open in front of you — every value in it becomes an
  environment variable. **`config.py` itself is never committed.**

Two things to know up front, because they shape the whole setup:

- **Railway blocks outbound SMTP on Free and Hobby.** That is why `mailer.py`
  sends through ZeptoMail's HTTPS API by default. You do not need the Pro plan.
- **The container filesystem is wiped on every deploy.** The database therefore
  lives on a mounted volume, and `settings.py` reads `DATA_DIR` to find it.

---

## Step 1 — Assemble the folder

Make a clean folder — do not reuse the dashboard folder, it has years of clutter:

```
D:\HRP\hrp-reporting-railway\
```

Copy in from this package:

```
scheduler.py          settings.py       mailer.py
crm_sync.py           requirements.txt  Procfile
railway.json          runtime.txt       .gitignore
make_env.py           RAILWAY.md
hrp_report\           (the whole folder)
```

There is **no `config.py`** in this bundle, and that is intentional — see
Step 2.

Do **not** copy `config.py`, `hrp_crm.db`, `reports\`, `logs\`, or any of the
`*.bat` files. The `.gitignore` already excludes them, but leaving them out
avoids accidents.

## Step 2 — Verify it runs locally first

**`config.py` is not in this bundle on purpose** — it holds your Zoho refresh
token and ZeptoMail key, and it must never reach GitHub. Copy your existing one
in temporarily so the local fallback works:

```powershell
cd D:\HRP\hrp-reporting-railway
copy "D:\HRP\CEO Dashboard\hrp_ceo_dashboard_2\hrp_dashboard\config.py" .
```

Then:

```powershell
pip install -r requirements.txt
python -c "import settings; print(settings.summary())"
```

Every field should be populated. Then test the new mail path, which is the one
piece most likely to need adjusting:

```powershell
python mailer.py
```

That sends a one-line test through the **ZeptoMail API**, not SMTP. If it fails
with an authorisation error, see Troubleshooting below. Fix this now — it is
much harder to diagnose once it is running in a container.

### Generate the Railway variables while config.py is still here

Do this now, before deleting it — it saves retyping a refresh token by hand,
which is where mistakes come from:

```powershell
python make_env.py --masked     # check the shape, secrets hidden
python make_env.py              # the real block, ready to paste
```

Copy that output somewhere safe for Step 6. It contains live credentials, so
keep it out of email, tickets and chat.

### Then remove it

```powershell
del config.py
```

From this point the folder has no secrets in it, which is what makes Step 3
safe.

## Step 3 — Push to GitHub

```powershell
cd D:\HRP\hrp-reporting-railway
git init
git add .
git commit -m "HRP CEO reporting service"
```

Confirm no secrets went in:

```powershell
git ls-files | Select-String "config.py|\.db|\.env"
```

That must return **nothing**. Then create an empty **private** repository on
GitHub and:

```powershell
git remote add origin https://github.com/<you>/hrp-reporting.git
git branch -M main
git push -u origin main
```

## Step 4 — Create the Railway project

1. railway.com → **New Project** → **Deploy from GitHub repo**
2. Pick the repository. Railway detects Python and builds automatically.
3. The first deploy will start and then sit idle — it has no configuration yet.
   That is expected.

## Step 5 — Add the volume (do this before the next deploy)

In the service → **Variables/Settings** → **Volumes** → **New Volume**:

- Mount path: `/data`
- Size: 1 GB is ample; the database is about 14 MB.

Without this, `hrp_crm.db` is destroyed on every deploy and the reports quietly
restart from an empty history.

## Step 6 — Set the environment variables

Service → **Variables** → **Raw Editor**, and paste the block below with your
real values substituted from `config.py`:

```
CLIENT_ID=<from config.py>
CLIENT_SECRET=<from config.py>
REFRESH_TOKEN=<from config.py>
ZOHO_TOKEN_URL=https://accounts.zoho.in/oauth/v2/token
ZOHO_CRM_BASE=https://www.zohoapis.in/crm/v2

COMPANY_NAME=Horizon Renewable Power
SMTP_FROM=<the verified ZeptoMail sender address>
RECIPIENTS=sauravarora@horizonrenewablepower.com,natasha@horizonrenewablepower.com,siddharth@horizonrenewablepower.com,sabina@horizonrenewablepower.com,smzk07@gmail.com
ALERT_TO=smzk07@gmail.com

MAIL_TRANSPORT=api
ZEPTO_TOKEN=<the ZeptoMail mail-agent token — same value as SMTP_PASSWORD>
ZEPTO_API_URL=https://api.zeptomail.in/v1.1/email

DATA_DIR=/data
REPORTS_DIR=/data/reports

TIMEZONE=Asia/Kolkata
MORNING_AT=08:00
SHIFT_TIMES=11:00,20:00
HEALTHCHECK_AT=21:00
MORNING_ENABLED=true
SHIFT_ENABLED=true
EVENING_ENABLED=false
HEALTHCHECK_ENABLED=true
```

Note that `RECIPIENTS` is comma-separated — `settings.py` splits it. And
`ALERT_TO` is set explicitly so watchdog alerts can never reach the CEO list.

**Times stay in IST.** The container runs UTC, but `scheduler.py` builds an
`Asia/Kolkata` scheduler, so you write the times you actually want.

## Step 7 — Deploy and watch

Railway redeploys automatically when variables change. Open **Deployments →
View Logs**. You want to see:

```
Config: CRM base https://www.zohoapis.in/crm/v2 | token set | transport api | ...
Morning report scheduled 08:00 Asia/Kolkata
Shift report scheduled 11:00 Asia/Kolkata
Shift report scheduled 20:00 Asia/Kolkata
Healthcheck scheduled 21:00 Asia/Kolkata
```

If `token MISSING` or `from MISSING` appears, a variable name is wrong.

## Step 8 — Seed the database and prove a real run

The volume starts empty. Force one full run immediately rather than waiting
until tomorrow morning: add the variable

```
RUN_ON_BOOT=shift
```

Save. The service restarts, syncs Zoho from scratch (a few minutes on the
first run, since it pulls 90 days across six modules), builds the PDF and sends
it. Watch the logs.

When the email arrives, **delete `RUN_ON_BOOT`** so it does not re-send on every
future deploy.

You do not need to copy the old `hrp_crm.db` across. A fresh sync reproduces the
same window the reports actually use.

## Step 9 — Check the status endpoint

Service → **Settings → Networking → Generate Domain**. Then open that URL:

```json
{
  "service": "hrp-reporting",
  "schedule": { "morning": "08:00", "evening": "18:30", "healthcheck": "21:00" },
  "last_runs": { "evening": { "at": "...", "status": "ok" } },
  "db_exists": true
}
```

`db_exists: true` confirms the volume is mounted correctly. Bookmark this — it
answers "is it alive?" in one glance.

## Step 10 — Retire the Windows tasks

Only after two consecutive successful days on Railway, so you always have a
working fallback:

```powershell
schtasks /change /tn "HRP Daily CEO Report"   /disable
schtasks /change /tn "HRP Evening CEO Report" /disable
schtasks /change /tn "HRP Report Healthcheck" /disable
```

Disable rather than delete. If Railway disappoints, re-enable and you are back
where you started in thirty seconds.

## Step 11 — Changing things afterwards

- **A report's time:** edit `EVENING_AT` in Variables. Railway restarts and the
  new time takes effect. No code, no deploy from your machine.
- **Turn a report off:** set `EVENING_ENABLED=false`.
- **Recipients:** edit `RECIPIENTS`.
- **Shift times:** edit `SHIFT_TIMES` — a comma-separated list. Adding a third
  entry gives three editions a day, and each still covers the interval since the
  previous one. No code change.
- **Metrics:** still a code change — that is what the admin console would fix.

---

## Troubleshooting

**`ZeptoMail API returned HTTP 401`** — the token is wrong or lacks the scheme
prefix. `mailer.py` adds `Zoho-enczapikey` automatically, so paste the raw token
only. Confirm in ZeptoMail → your Mail Agent → SMTP/API → Send Mail Token.

**`ZeptoMail API returned HTTP 404`** — wrong regional endpoint. Your account is
on the India data centre (`smtp.zeptomail.in`), so `api.zeptomail.in` should be
right; if not, try `https://api.zeptomail.com/v1.1/email`.

**Emails send but land in spam** — unchanged from before. SPF and DKIM are
already correct for your domain; the sending IP is now Railway's rather than
your office, so expect a short reputation warm-up.

**`db_exists: false`** — the volume is not mounted at `/data`, or `DATA_DIR`
does not match the mount path.

**Reports show zeros** — the sync failed but the report still built. The PDF
prints its snapshot age, and the evening report shows the red staleness banner.
Check the logs for `OAUTH_SCOPE_MISMATCH` or a token failure.

**Service keeps restarting** — read the top of the log. A missing variable that
`settings.py` needs will surface as a `NoneType` error on the first job.

---

## What this does not fix

Moving to Railway makes the *schedule* reliable. It does not improve the data:
per-person call attribution is still inferred, meetings still read zero until
the team logs them, and `Amount` is still blank on most open deals. Those are
Zoho configuration problems and they travel with you.

It also introduces one new dependency. If Railway has an outage, the reports
stop, and unlike the office machine you cannot walk over and restart it. The
healthcheck still runs on Railway, so it shares that blind spot — the only real
cover is noticing that the 08:00 email did not arrive.
