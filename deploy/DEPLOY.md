# BookaBL deployment on Hetzner Ubuntu

These commands assume DNS for `bookabl.co.za` points to the server and the repository is deployed
to `/srv/bookabl`.

## 1. Install system packages

```bash
apt update
apt install -y git nginx certbot python3-certbot-nginx python3.12 python3.12-venv
```

## 2. Clone and install BookaBL

```bash
git clone <repository-url> /srv/bookabl
cd /srv/bookabl
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

Copy the production environment file to `/srv/bookabl/.env`, fill in every required secret, and
restrict access to it:

```bash
cp /path/to/production.env /srv/bookabl/.env
chmod 600 /srv/bookabl/.env
```

Apply the repository migrations to the production Supabase project before starting the services.

## 3. Install and start the systemd services

```bash
cp /srv/bookabl/deploy/bookabl-api.service /etc/systemd/system/
cp /srv/bookabl/deploy/bookabl-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bookabl-api bookabl-worker
systemctl status bookabl-api bookabl-worker
```

## 4. Configure Nginx and HTTPS

The supplied final Nginx configuration references the certificate paths that Certbot creates. On a
new server, first expose a temporary HTTP-only `bookabl.co.za` server block, then request the
certificate:

```bash
certbot --nginx -d bookabl.co.za
```

Install the supplied HTTPS configuration after the certificate exists:

```bash
cp /srv/bookabl/deploy/nginx-bookabl.conf /etc/nginx/sites-available/bookabl
ln -sfn /etc/nginx/sites-available/bookabl /etc/nginx/sites-enabled/bookabl
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

Check the deployment with `curl -I https://bookabl.co.za/health` and inspect service logs with:

```bash
journalctl -u bookabl-api -u bookabl-worker -f
```
