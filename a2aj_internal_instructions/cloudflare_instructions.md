# Cloudflare Tunnel Setup

Cloudflare Tunnel exposes a service running on localhost to a public hostname over HTTPS, without opening inbound ports on the VM and without managing TLS certs yourself. The connection is initiated outbound from the VM to Cloudflare's edge, so the VM stays firewalled.

These instructions are generic — substitute your own tunnel name, UUID, hostname, and domain wherever placeholders appear.

## Prerequisites

- A Cloudflare account
- A domain whose DNS is hosted on Cloudflare
- The service you want to expose already running locally (e.g., `uvicorn` on `127.0.0.1:8000`)

## Important: which user to run these commands as

Run all `cloudflared tunnel ...` commands (login, create, route dns, list, info) as your **regular user** (e.g., `ubuntu`), **not** as root. The cert and credentials files land in that user's `~/.cloudflared/` directory, and `cloudflared` looks there by default. Running these as root puts files into `/root/.cloudflared/`, which then breaks the rest of the workflow.

`sudo` is only needed for system-level steps:

- Installing the `.deb` package
- `cloudflared service install` / `service uninstall`
- Editing files under `/etc/cloudflared/`
- `systemctl` operations

If you accidentally run `cloudflared tunnel login` (or any other tunnel command) as root and create files in `/root/.cloudflared/`, the cleanest recovery is to redo the step as your regular user. You can also point one-off commands at the right cert with `--origincert /home/<USER>/.cloudflared/cert.pem`, but consistency is easier.

## 1. Install cloudflared

```bash
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
rm cloudflared-linux-amd64.deb
```

## 2. Authenticate against your Cloudflare account

Run as your regular user (not root):

```bash
cloudflared tunnel login
```

The command prints a URL. Open it in a browser logged into your Cloudflare account, select the zone you want to use, and authorize. A certificate is written to `~/.cloudflared/cert.pem`.

## 3. Create a tunnel

```bash
cloudflared tunnel create <TUNNEL-NAME>
```

Pick a descriptive name (e.g., `legal-api-tunnel`). Tunnel names must be unique within your Cloudflare account, so if you're running multiple instances of this project, give each one its own tunnel name.

The command prints a **Tunnel UUID** and writes credentials to `~/.cloudflared/<TUNNEL-UUID>.json`. Note the UUID — it's used in the next two steps.

## 4. Configure ingress (working draft in your home directory)

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /home/<USER>/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: <HOSTNAME>.<DOMAIN>
    service: http://localhost:8000
  - service: http_status:404
```

Substitute:

- `<TUNNEL-UUID>` — the UUID from step 3
- `<USER>` — the Linux user whose home directory holds the credentials file (e.g., `ubuntu`)
- `<HOSTNAME>.<DOMAIN>` — the public hostname you want (e.g., `api.example.com`)

Replace **every** placeholder. cloudflared will literally try to open a file called `<TUNNEL-UUID>.json` if you forget to substitute the UUID.

The trailing `service: http_status:404` block is a required catch-all.

To expose more than one local service through the same tunnel, add additional ingress blocks above the catch-all. For example, to also expose a standalone MCP server on port 8001:

```yaml
ingress:
  - hostname: <HOSTNAME>.<DOMAIN>
    service: http://localhost:8000
  - hostname: <MCP-HOSTNAME>.<DOMAIN>
    service: http://localhost:8001
  - service: http_status:404
```

Each public hostname needs its own DNS record in step 5.

## 5. Create the DNS record

The fastest way is the cloudflared CLI (run as your regular user):

```bash
cloudflared tunnel route dns <TUNNEL-NAME> <HOSTNAME>.<DOMAIN>
```

This creates a proxied CNAME from `<HOSTNAME>.<DOMAIN>` to `<TUNNEL-UUID>.cfargotunnel.com`.

If a CNAME already exists for that hostname (pointing somewhere else, or at a different tunnel), add `-f` to overwrite it:

```bash
cloudflared tunnel route dns -f <TUNNEL-NAME> <HOSTNAME>.<DOMAIN>
```

Alternatively, do it through the Cloudflare dashboard: DNS → Add Record → CNAME, name `<HOSTNAME>`, target `<TUNNEL-UUID>.cfargotunnel.com`, proxy status **Proxied** (orange cloud).

## 6. Test the tunnel manually

```bash
cloudflared tunnel run <TUNNEL-NAME>
```

Leave this running in the foreground. From any machine, hit the public URL:

```bash
curl -I https://<HOSTNAME>.<DOMAIN>/health
```

A `200 OK` confirms the full path: client → Cloudflare edge → tunnel → `localhost:8000` → response. If you get a `502`, the tunnel reached your VM but couldn't connect to the local service — confirm the local service is running and listening on the expected port. Stop the manual tunnel with `Ctrl+C` before continuing.

> **On Windows**, `curl -I` may fail with `CRYPT_E_NO_REVOCATION_CHECK`. Add `--ssl-no-revoke` to the curl command, or just open the URL in a browser. This is a Windows TLS quirk, not a server issue.

## 7. Install as a systemd service (the production config lives in /etc/)

```bash
sudo cloudflared service install
sudo systemctl status cloudflared
```

The installer creates and enables `cloudflared.service`. **By default the systemd unit reads config from `/etc/cloudflared/config.yml`, not from your home directory.** This is the most common gotcha: edits to `~/.cloudflared/config.yml` won't take effect once the service is installed, because the service is reading a different file.

Confirm where the running service is reading from:

```bash
systemctl cat cloudflared | grep ExecStart
```

Look for `--config /etc/cloudflared/config.yml`. That's the file that matters.

Copy your working draft into `/etc/cloudflared/`, along with the credentials file:

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/config.yml
sudo cp ~/.cloudflared/<TUNNEL-UUID>.json /etc/cloudflared/<TUNNEL-UUID>.json
```

Edit the `credentials-file` line in `/etc/cloudflared/config.yml` to point at the new path:

```bash
sudo nano /etc/cloudflared/config.yml
```

```yaml
credentials-file: /etc/cloudflared/<TUNNEL-UUID>.json
```

Restart so the service picks up the config:

```bash
sudo systemctl restart cloudflared
```

## 8. Verify

```bash
sudo systemctl status cloudflared
journalctl -u cloudflared -n 50
curl -I https://<HOSTNAME>.<DOMAIN>/health
```

The service should be `active (running)`. The journal should show four "Registered tunnel connection" entries (Cloudflare establishes redundant connections to multiple edge POPs). The curl should return `200 OK`.

## Day-to-day operations

### Editing the running config

Edits must land in `/etc/cloudflared/config.yml`, which requires `sudo`. Two patterns work:

**Pattern A — edit `/etc/` directly:**

```bash
sudo nano /etc/cloudflared/config.yml
sudo systemctl restart cloudflared
```

**Pattern B — edit your home draft, then sync:**

```bash
nano ~/.cloudflared/config.yml          # no sudo needed
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/config.yml
sudo systemctl restart cloudflared
```

Pattern B keeps your edit cycle in user-space and lets you use editors like VS Code without elevated permissions, but you have to remember the sync step or your changes won't take effect.

If you forget to restart cloudflared after editing, the new ingress entries will return Cloudflare's catch-all 404 even though your config file looks correct.

### Useful commands

```bash
sudo systemctl restart cloudflared           # after editing /etc/ config
journalctl -u cloudflared -f                 # tail logs live
cloudflared tunnel list                      # list your tunnels
cloudflared tunnel info <TUNNEL-NAME>        # show tunnel details + active connections
```

### Adding a new hostname to an existing tunnel

1. Edit `/etc/cloudflared/config.yml`, add a new `- hostname: ... service: ...` block above the catch-all
2. `sudo systemctl restart cloudflared`
3. `cloudflared tunnel route dns <TUNNEL-NAME> <NEW-HOSTNAME>.<DOMAIN>` (or `-f` to overwrite an existing record)

### Cutting a hostname over from one tunnel to another

To re-point an existing hostname (e.g., to a tunnel on a different VM) without dashboard work:

```bash
cloudflared tunnel route dns -f <NEW-TUNNEL-NAME> <HOSTNAME>.<DOMAIN>
```

The `-f` overwrites the existing CNAME. Make sure the new tunnel's `/etc/cloudflared/config.yml` already has the hostname in its ingress before flipping DNS, or you'll briefly serve Cloudflare 404s.

## Removing or replacing a tunnel

```bash
cloudflared tunnel delete <TUNNEL-NAME>
```

This removes the tunnel from Cloudflare but does not delete the DNS record — clean that up in the dashboard, or replace it with `cloudflared tunnel route dns` pointing at a different tunnel.

## Common pitfalls (recap)

| Symptom | Cause | Fix |
|---|---|---|
| `Tunnel credentials file '<PASTE-UUID-HERE>.json' doesn't exist` | Forgot to substitute placeholder values in `config.yml` | Edit the config, replace every `<...>` with real values |
| `Cannot determine default origin certificate path` when running as root | Ran tunnel command as root; cert lives in `/home/<USER>/.cloudflared/` | Run as the regular user, or pass `--origincert /home/<USER>/.cloudflared/cert.pem` |
| Edits to `~/.cloudflared/config.yml` don't take effect | systemd service reads `/etc/cloudflared/config.yml`, not your home directory | Edit `/etc/` (with `sudo`) or sync your home file there |
| Cloudflare returns `404 Not Found` with `Server: cloudflare` | Hostname has a DNS CNAME but isn't in the tunnel's ingress block | Add it to ingress and restart cloudflared |
| Cloudflare returns `502 Bad Gateway` | Tunnel reached the VM but local service isn't responding on the configured port | `systemctl status` the local service; check it's listening on the expected port |
| MCP/SSE clients can't connect even though `curl` works | Cloudflare proxy interfering with streaming, or hitting bot/security checks | Compare zone Configuration Rules / Security Events between working and non-working hostnames; replicate any host-specific rules |