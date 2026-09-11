# Connecting to the R2 Data Bucket

Getting you read access to our market data. About 10 minutes.

## 0. What I'll send you

A **personal, read-only** R2 key — three values:

- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

They're yours alone; please don't forward them. If someone else needs access, ask me
and I'll issue them their own so keys can be revoked individually.

The key is read-only and scoped to the `msfe-practicum` bucket. You can't delete,
overwrite or corrupt anything with it, so don't be nervous about poking around.

## 1. Install

```bash
brew install rclone direnv
echo 'eval "$(direnv hook zsh)"' >> ~/.zshrc     # bash: direnv hook bash, ~/.bashrc
exec zsh
```

**Don't skip the shell hook.** Installing direnv without it does nothing at all,
silently, and you'll spend twenty minutes wondering why your environment variables
are empty. I did. You'll know it's working when you see `direnv: loading .../.envrc`
on `cd` into the repo.

## 2. Pull `main` first

```bash
git checkout main && git pull
```

This matters: `.gitignore` has no effect on files git is already tracking, so the
rule that ignores `secrets/` needs to be in your working tree **before** you create
the directory below.

## 3. Drop your credentials in

Both files go in `secrets/`, which is gitignored. Two formats of the same key,
because rclone won't read the dotenv one.

`secrets/r2.env` — plain `KEY=value`, no `export`, no spaces around `=`:

```bash
mkdir -p secrets
cat > secrets/r2.env <<'EOF'
R2_ACCOUNT_ID=<your value>
R2_ACCESS_KEY_ID=<your value>
R2_SECRET_ACCESS_KEY=<your value>
R2_BUCKET=msfe-practicum
R2_ENDPOINT=https://<your account id>.r2.cloudflarestorage.com
EOF
```

`secrets/rclone.conf`:

```bash
cat > secrets/rclone.conf <<'EOF'
[r2]
type = s3
provider = Cloudflare
access_key_id = <your value>
secret_access_key = <your value>
endpoint = https://<your account id>.r2.cloudflarestorage.com
region = auto
no_check_bucket = true
EOF
```

Watch for trailing whitespace and stray quotes — rclone's INI parser takes them
literally and the resulting error looks like a bad key.

Then activate:

```bash
direnv allow
```

`.envrc` is already in the repo and holds no secrets — it sources `secrets/r2.env`
and points `RCLONE_CONFIG` at `secrets/rclone.conf`. Your environment now loads on
`cd` into the repo and unloads when you leave. Re-run `direnv allow` any time
`.envrc` changes.

**Never commit a key.** `secrets/` is ignored, but `git add .` is one typo away from
permanent history. If you ever do commit one, tell me straight away so I can rotate
it — deleting it in a later commit doesn't unleak it. Takes thirty seconds to fix,
nobody will be annoyed.

## 4. Test it works

```bash
rclone ls r2:msfe-practicum/raw/trades | head -5
rclone size r2:msfe-practicum/raw/trades --fast-list
```

Expected: a few filenames, then a four-figure object count. Don't match on the exact
numbers —
it changes as we correct files and extend the series. `docs/data-inventory.md` carries
the current figures.

Now test the Python path too — rclone working does **not** prove the dotenv file
loaded, and they fail independently:

```bash
pip install boto3
python -c "
import os, boto3
c = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'],
                 aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                 aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
                 region_name='auto')
r = c.list_objects_v2(Bucket=os.environ['R2_BUCKET'], Prefix='raw/', MaxKeys=5)
print([o['Key'] for o in r['Contents']])
"
```

Both commands returning data means you're done.

**`rclone lsd r2:` returning 403 AccessDenied is normal.** Your token is scoped to
one bucket, so listing *all* buckets is denied. Per-bucket commands work fine. Not a
broken key — see section 5 for the error that is.

## 5. What's in the bucket

Databento DBN under zstd — **not parquet**, so `read_parquet` won't open it. Use the
`databento` package. One prefix, `raw/`, 2,703 objects, roughly 48 GiB. These counts
move as the series are extended; `docs/data-inventory.md` is generated and is the live
source. Don't match on a total in a script.

| Prefix | Data files | Notes |
|---|---|---|
| `raw/trades` | 1,056 | tick trades, NQ + ES — our primary price source |
| `raw/opra_cbbo1m` | 548 | QQQ options quotes, per-minute; feeds GEX only |
| `raw/opra_definition` | 548 | options instrument definitions |
| `raw/opra_statistics` | 548 | options statistics |

**Plus 3 sidecar objects under `raw/opra_definition/`** — `condition.json`,
`manifest.json`, `metadata.json`. They are vendor bookkeeping, not data, which is why
that prefix lists 551 keys while holding 548 data files. Counting the sidecars as data
is what made an earlier version of this table say 515; if a number here is three larger
than you expect, that is why.

Coverage: NQ 684 file-dates with one gap (2025-04-18), ES 372 from 2025-07-03, QQQ
options 548. **All three series now run to 2026-09-09** — the futures/options gap that
opened in July 2026 is closed.

Things to know before you query:

- **Bars are built from `trades`**, not from a vendor bar file. Session boundaries,
  VWAP anchoring (17:00 CT Globex open) and roll conventions live in versioned code so
  they're reviewable. Please don't introduce a second bar source — that's how you end
  up with two sets of conventions and no way to tell which produced a result.
- **`opra_cbbo1m` is 38 GiB.** Don't scan it casually. It feeds the GEX build and
  nothing else.
- **`raw/` was mirrored from our source data lake on 2026-09-08 and intentionally
  diverges from it on six objects.** Six NQ files for 2025-07-03…09 originally
  contained ES data and were corrected in place the same day.
  `config/corrections.yaml` is the record and `docs/data-inventory.md` §9 explains it.
  Bucket integrity is now verified against `data/manifest.json` rather than against
  the lake.
- **The layout will change** — the holdout period gets split into a separate bucket
  before our first agent run. Don't hardcode paths; go through the loader.

Read `CLAUDE.md` before writing code against any of this. It carries the data
invariants — DBN version mixing, UTC-day file splits, header-over-path symbol
identity — that will bite you otherwise.

## 6. Troubleshooting

**`403 AccessDenied` on `rclone lsd r2:`** — expected, ignore it.

**`401 Unauthorized`** — the credential wasn't recognised. Three usual causes:

1. You pasted Cloudflare's *Token value* instead of the *Access Key ID*. The token
   screen shows four fields; only Access Key ID and Secret Access Key belong in an
   S3 client.
2. A typo, a leftover `<your value>` placeholder, or trailing whitespace.
3. `RCLONE_CONFIG` isn't set, so rclone fell back to `~/.config/rclone/rclone.conf`.

Isolate #3 with:
```bash
rclone --config secrets/rclone.conf ls r2:msfe-practicum/raw/trades | head -3
```
If that works but bare `rclone` doesn't, it's a direnv problem, not a key problem.

**`KeyError: 'R2_ENDPOINT'`** — direnv isn't loaded. Run `direnv allow`, confirm the
hook is in `~/.zshrc`, open a new terminal. No `direnv: loading` message on `cd`
means the hook is missing.

**`echo $RCLONE_CONFIG` is empty** — same thing, direnv isn't loaded.

**A brand-new key 401s** — R2 tokens can take up to a minute to propagate. Wait and
retry once before debugging.

**Slow transfers** — add `--fast-list` for listing, `--transfers 16` for copies. R2
charges no egress, so pulling data costs time and nothing else.

**Need data we don't have?** Ask me — the Databento account is tied to my login, so
all downloads go through me. Don't try to acquire it separately; we'd end up with two
provenance trails.

## 7. Where downloads go — **not in the repo**

Vendor downloads land **outside the working tree**, in `~/dl/databento/`, and are
uploaded to R2 from there. Never into the repo, not even temporarily: a couple of GiB of
tick data inside the working tree is one `git add -A` from a very bad afternoon, and
unlike a leaked key you cannot rotate a 2 GiB blob out of history quickly.

`ext/` is in `.gitignore` purely as a guard for that habit — the directory does not
exist and should not be recreated. The repo holds the *derived, committed* artifacts
(`data/manifest.json`, `data/nq-contract-selection.json`) and the session cache under
`data/cache/`, which is gitignored; the raw bytes live in the bucket.