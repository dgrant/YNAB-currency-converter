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

```bash
git pull
docker compose up -d --build
```

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

## Future: auto-deploy on push (GitHub Actions)

Generate a deploy key pair, put the public half in the server's
`~/.ssh/authorized_keys`, add the private half as a repo secret
(`SSH_KEY`) plus `SSH_HOST`/`SSH_USER`, and add a workflow that runs
`git pull && docker compose up -d --build` over SSH on push to master.
Not set up yet — ask Claude to add it when wanted.
