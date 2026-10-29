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

Expected: a few filenames, then `Total objects: 952 / Total size: 5.140 GiB`.

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

## 5. Troubleshooting

**`403 AccessDenied` on `rclone lsd r2:`** — expected, ignore it. Your token is
scoped to one bucket, so listing *all* buckets is denied. Per-bucket commands work.

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