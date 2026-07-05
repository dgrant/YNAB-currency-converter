# Deploying to a VPS (Linode)

## Prerequisites

- Docker + the compose plugin on the server:
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```

## Deploy

```bash
git clone https://github.com/dgrant/YNAB-currency-converter.git
cd YNAB-currency-converter
cp .env.example .env
nano .env        # set APP_PASSWORD, SECRET_KEY, YNAB_TOKEN
docker compose up -d --build
```

The app now listens on `127.0.0.1:8000` (localhost only — not reachable
from the internet until you set up one of the access options below). `data/conversions.json` (the only persistent
state) lives in `./data` on the host — back it up if you care about your
conversion configs; losing it never touches your YNAB data.

## Updating

Auto-deploy (below) normally handles this. To update by hand:

```bash
git pull
docker compose up -d --build
```

## Auto-deploy on push

Merging/pushing to the default branch deploys automatically:

1. GitHub Actions (`.github/workflows/ci.yml`) runs pytest on every push.
2. A cron job on the server runs `deploy/autodeploy.sh` every 2 minutes. When
   the default branch has new commits *and* their CI checks are green, it
   fast-forwards and runs `docker compose up -d --build`, then health-checks
   `/login`. Commits with failing CI are never deployed.

There are no deploy secrets: the server polls GitHub over public HTTPS
(`git fetch` + the unauthenticated check-runs API); nothing connects in.
Worst-case latency from merge to live is ~2 minutes plus the build.

One-time setup on the server:

```bash
cd ~/YNAB-currency-converter && git pull
( crontab -l 2>/dev/null; echo '*/2 * * * * flock -n $HOME/.ynabfx-deploy.lock $HOME/YNAB-currency-converter/deploy/autodeploy.sh >> $HOME/autodeploy.log 2>&1' ) | crontab -
```

The script is silent when there's nothing to do; deploys, CI waits/failures,
and health-check results are appended to `~/autodeploy.log`:

```bash
tail ~/autodeploy.log
```

To pause auto-deploy, comment out the line with `crontab -e`.

## Exposing it safely

The app has password auth, but don't serve it over plain HTTP on the open
internet. Pick one:

**Option A — keep it private (simplest):** the app already binds to
localhost only; reach it through an SSH tunnel when you need it:

```bash
ssh -L 8000:localhost:8000 you@your-linode
# then browse http://localhost:8000
```

**Option B — HTTPS with Caddy:** if you have a domain pointed at the server:

```bash
apt install caddy
```

`/etc/caddy/Caddyfile`:

```
ynab.yourdomain.com {
    reverse_proxy localhost:8000
}
```

Caddy fetches the TLS certificate automatically. If you already run nginx,
an equivalent `proxy_pass http://localhost:8000;` server block plus
certbot works too.

## Getting SSH access without a local key

Linode's **Lish** web console (Linode admin panel → your Linode → "Launch
LISH Console") gives you a terminal even with no SSH key set up. From there
you can add a public key so normal SSH works afterwards:

```bash
mkdir -p ~/.ssh && echo 'ssh-ed25519 AAAA... you@laptop' >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
```

## Alternative considered: push-based deploy over SSH

The classic setup — a GitHub Actions job that SSHes into the server with a
deploy key and runs `git pull && docker compose up -d --build` — would deploy
seconds after merge instead of within ~2 minutes. It was skipped because it
requires storing an SSH private key for the server as a secret in a public
repo, and the poller above needs no credentials in either direction. Revisit
if the 2-minute latency ever matters.
