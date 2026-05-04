# clamav-mirror

Auto-updated mirror of ClamAV core databases (`daily.cvd`, `bytecode.cvd`).

**Why this exists:** the official ClamAV CDN (`database.clamav.net`) serves
HTTP 403 to several IP ranges (RU/KZ/DE/BY VPS providers). GitHub-hosted
runners can still reach the CDN, so we use Actions to pull databases and
republish them as a release asset that any consumer can fetch.

## Schedule

GitHub Actions runs every 2 hours (`cron: 15 */2 * * *`). Each run:

1. Downloads `daily.cvd` and `bytecode.cvd` from the official CDN.
2. If MD5 differs from the current `latest` release, publishes a new release.
3. Otherwise no-op (no needless asset churn).

## Consumer

```bash
BASE="https://github.com/Raul-1996/clamav-mirror/releases/latest/download"
curl -fsSL -o /tmp/MD5SUMS "$BASE/MD5SUMS"
for db in daily.cvd bytecode.cvd; do
  curl -fsSL -o /tmp/$db "$BASE/$db"
  grep " $db$" /tmp/MD5SUMS | md5sum -c -
done
```

## Notes

- `main.cvd` is intentionally **not** mirrored — it's regenerated rarely
  (months) and is ~90 MB; keep using whatever you already have.
- We do not mirror unofficial signature feeds (sanesecurity etc.) — those
  reach Mailcow directly without CDN block.
