# Running on Render (free) + cron-job.org

Render runs `server.py`, a tiny web service. A free cron service
(cron-job.org) starts the daily scrape and keeps Render awake while it runs.
Data goes only to CockroachDB.

```
cron-job.org ──06:00 IST── GET /run ───────▶ Render (Singapore, free)
cron-job.org ──every 10 min, 05:50–09:30── GET /health   (keeps it awake)
                                             │ runs one state at a time:
                                             │ tamilnadu → karnataka → maharashtra
                                             ▼
                                         CockroachDB
```

Why the pings: Render free services go to sleep after 15 minutes without
web traffic, even in the middle of a job. A run takes about 1.5 hours
(Maharashtra alone ~1 hour), so something must call `/health` at least every
15 minutes until it finishes. Pinging only in the morning window keeps you
well inside the 750 free hours a month.

## 1. Deploy (once, ~10 minutes)

1. Sign in at https://render.com with your GitHub account.
2. **New → Blueprint**, pick the `RERA-Scheduler` repository, click **Apply**.
   It reads `render.yaml`: a free web service named `rera-scraper` in Singapore.
3. Render asks for **DATABASE_URL**: paste the same value as in your `.env`.
4. Wait for the first deploy to show **Live**. Copy the service URL,
   e.g. `https://rera-scraper.onrender.com`.
5. In the service, open **Environment** and copy the value of **RUN_TOKEN**
   (Render generated it). You need it for the cron job. Don't share it.

## 2. Check it can reach the portals (most important)

Render has no India region, and these government portals block some foreign
servers (GitHub's US servers were blocked). Open, in your browser:

```
https://rera-scraper.onrender.com/probe?token=YOUR_RUN_TOKEN
```

| Result | Meaning |
|---|---|
| all three `"reachable": true` | Go ahead |
| any `"reachable": false` | That portal blocks Render's Singapore servers - Render will not work for it. Stop here and keep the GitHub Actions + laptop setup. |

## 3. Test one run

```
https://rera-scraper.onrender.com/run?states=karnataka&token=YOUR_RUN_TOKEN
```
Answer: `"started": true`. After ~5 minutes check:
```
https://rera-scraper.onrender.com/status?token=YOUR_RUN_TOKEN
```
`"exit_code": 0` and a `Database: ...` line in `log_tail` means it worked.
(The service may sleep during this test after 15 minutes - Karnataka finishes
well before that.)

## 4. Set up cron-job.org (free)

Create an account at https://cron-job.org, then **Create cronjob** twice.
Set the time zone to **Asia/Kolkata** in your account settings first.

**Job 1 - start the daily run**
- URL: `https://rera-scraper.onrender.com/run`
- Schedule: every day at **06:00**
- Advanced → Headers: add `X-Run-Token` = your RUN_TOKEN
  (safer than putting the token in the URL)
- Request timeout: 60 seconds or more (a sleeping service takes ~1 minute to wake)

**Job 2 - keep it awake while running**
- URL: `https://rera-scraper.onrender.com/health`
- Schedule: custom - every **10 minutes**, hours **5 to 9** (05:50–09:50)
- No headers needed

Every 3-5 minutes also works, but 10 is enough: Render only sleeps after 15
minutes without traffic.

## 5. Check every day

Same SQL as before, in the CockroachDB console:
```sql
SELECT state_code, records_inserted, records_updated, records_failed, completed_at
FROM scrape_runs WHERE completed_at > now() - INTERVAL '1 day' ORDER BY completed_at;
```
Three rows (TN, KA, MH) = healthy. Or open `/status?token=...`.

## 6. Turn off the GitHub schedule (after Render works for a few days)

Otherwise both run every morning. GitHub → **Actions → RERA daily sync →
⋯ → Disable workflow**. You can re-enable it any time as a fallback.

## Limits of the free tier (know these)

- **512 MB memory.** The scrapers run with `RERA_DB_ONLY=1`: database only,
  one year/district/page-batch at a time, each parsed in a fresh process.
  Measured peak locally is listed in the commit message.
- **750 free hours/month** per Render workspace. The morning window uses
  ~4-5 hours a day. If you ping 24/7 instead, this one service uses almost
  all 750 hours.
- **Restarts.** Render may restart free services (deploys, maintenance).
  A run interrupted that way is simply redone the next day; nothing is
  duplicated because the database deduplicates by RERA number.
- **Render free web services are meant for web apps, not background jobs.**
  This setup works within their limits but is not what the free tier is
  designed for; a paid Render Cron Job (billed per minute of running time)
  is the clean alternative.
