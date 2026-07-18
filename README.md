# clamav-mirror

Verified mirror of the official ClamAV core databases:

- `daily.cvd`
- `main.cvd`
- `bytecode.cvd`

## Why this exists

The official ClamAV CDN (`database.clamav.net`) returns HTTP 403 for the
Mailcow server network. GitHub-hosted runners can update through Cisco's
`cvdupdate`, so Actions fetches the signed databases and republishes verified
snapshots as GitHub Releases.

## Publication model

`Update ClamAV DBs` runs every two hours. It:

1. Installs `cvdupdate` from a fully hashed dependency lock.
2. Downloads all three official databases.
3. Verifies every CVD signature with `sigtool --info`.
4. Rejects missing files, undersized files and database version regressions.
5. Produces deterministic `SHA256SUMS`, compatibility `MD5SUMS` and
   `SNAPSHOT.json`.
6. Transfers the verified snapshot to a separate publish job.
7. Creates a draft Release with an immutable `db-<snapshot-id>` tag.
8. Downloads every draft asset and compares it byte-for-byte with the local
   verified snapshot.
9. Publishes the draft and switches GitHub's `latest` designation only after
   verification succeeds.

A failed upload or failed read-back leaves at most an unpublished draft. The
previous public latest Release remains complete and usable.

## Consumer: FreshClam PrivateMirror

Do not copy CVD files directly into a running ClamAV database directory.
FreshClam provides staging, signature checks, database loading tests and clamd
notification.

```conf
PrivateMirror https://github.com/Raul-1996/clamav-mirror/releases/latest/download
TestDatabases yes
NotifyClamd /etc/clamav/clamd.conf
```

FreshClam first requests `.cld` and then falls back to `.cvd`. GitHub Release
assets support the redirects and HTTP Range requests required by FreshClam.
This behavior is tested against the same Mailcow clamd image used in
production.

## Reliability controls

- Workflow-level `concurrency` prevents overlapping publishers.
- Actions are pinned to full commit SHAs.
- Build and publish jobs use separate permissions; only publish receives
  `contents: write`.
- A minimal weekly keepalive workflow writes repository activity only after 30
  quiet days, reducing the risk of GitHub's 60-day public-repository schedule
  auto-disable.
- Keepalive is not treated as monitoring. An external watchdog must alert when
  the workflow is disabled or its last successful run is stale.

## Stable URL

Consumers use GitHub's latest-release endpoint:

```text
https://github.com/Raul-1996/clamav-mirror/releases/latest/download/daily.cvd
```

Do not use `/releases/download/latest/...`: `latest` is no longer a mutable tag;
it is the GitHub latest-Release designation pointing to an immutable snapshot.
