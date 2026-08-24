# Production deployment checklist

Auremgrid runs as a local-first Python process with SQLite. This checklist covers the minimum for a controlled single-host deployment.

## Before starting

- [ ] Run `scripts/prepare-deploy.ps1` to validate Python, .env, and DB path
- [ ] Copy `.env.example` to `.env`, edit local ids/paths, and keep provider secrets in environment or a vault
- [ ] Run `python scripts/private_host_smoke.py` on the host or release artifact; it must pass without Docker
- [ ] Run `scripts/release.py validate` to confirm the codebase passes all checks
- [ ] Run `auremgrid backup` and `auremgrid verify-backup` against the current database
- [ ] Rehearse restore on a copy: `auremgrid restore --backup ... --db ... --overwrite`

## Reverse proxy and TLS

- [ ] Install nginx or Caddy in front of the Python HTTP server
- [ ] Bind the Python server to `127.0.0.1` only (never `0.0.0.0` without a proxy)
- [ ] Configure TLS termination at the proxy layer
- [ ] Set `X-Forwarded-Proto` and `X-Forwarded-For` headers

## Firewall and access

- [ ] Restrict port 8791 (or chosen port) to the proxy or private network
- [ ] Do not expose the SQLite file path via any network-accessible endpoint
- [ ] Ensure `.env` and secret references are not in the repo or web root

## Backup and recovery

- [ ] Schedule `auremgrid backup` to run at least daily (cron / Task Scheduler)
- [ ] Schedule `auremgrid verify-backup` after each backup
- [ ] Rotate backup files (keep at least 7 daily, 4 weekly)
- [ ] Store backups on a separate filesystem or remote destination
- [ ] Test restore on a separate machine at least once per month

## Process management

- [ ] Run the web server and `worker-once` as separate processes
- [ ] Use a process supervisor (nssm, supervisord, systemd) for auto-restart
- [ ] Set `PYTHONPATH=src` in the process environment
- [ ] Configure log output to a file with rotation

## Monitoring

- [ ] Poll `/health` from an external monitor (uptime check)
- [ ] Check that `/health` or `/health/detailed` reports schema version 53 for this release line, or the later schema version shipped by the artifact being deployed
- [ ] Alert on process crashes, backup failures, or recovery mode activation

## Secrets

- [ ] All provider credentials in environment variables or a vault
- [ ] Never commit `.env` to the repository
- [ ] Rotate API keys and tokens on a regular schedule
- [ ] Provide your own OAuth client registrations and token-exchange transport before using OAuth routes; no provider client credentials ship with the repository
- [ ] Treat `Dockerfile`, `deploy/Caddyfile`, and `deploy/docker-compose.yml` as private single-host templates; certificate, firewall, browser-runtime, provider-connectivity, and access-policy operation remains yours
