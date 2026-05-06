# Cloudflare Tunnel Setup

Cloudflare Tunnel exposes a service running on localhost to a public hostname over HTTPS, without opening inbound ports on the VM and without managing TLS certs yourself. The connection is initiated outbound from the VM to Cloudflare's edge, so the VM stays firewalled.

These instructions are generic — substitute your own tunnel name, UUID, hostname, and domain wherever placeholders appear.

## Prerequisites

- A Cloudflare account
- A domain whose DNS is hosted on Cloudflare
- The service you want to expose already running locally (e.g., `uvicorn` on `127.0.0.1:8000`)

## 1. Install cloudflared

```bash
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
rm cloudflared-linux-amd64.deb
```

## 2. Authenticate against your Cloudflare account

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

## 4. Configure ingress

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

The fastest way is the cloudflared CLI:

```bash
cloudflared tunnel route dns <TUNNEL-NAME> <HOSTNAME>.<DOMAIN>
```

This creates a proxied CNAME from `<HOSTNAME>.<DOMAIN>` to `<TUNNEL-UUID>.cfargotunnel.com`.

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

## 7. Install as a systemd service

```bash
sudo cloudflared service install
sudo systemctl status cloudflared
```

The installer creates and enables `cloudflared.service`. By default it reads config from `/etc/cloudflared/config.yml`, not from your home directory. Copy your config there:

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/config.yml
sudo cp ~/.cloudflared/<TUNNEL-UUID>.json /etc/cloudflared/<TUNNEL-UUID>.json
```

Edit the `credentials-file` line in `/etc/cloudflared/config.yml` to point at the new path:

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

```bash
sudo systemctl restart cloudflared           # after editing config
journalctl -u cloudflared -f                 # tail logs live
cloudflared tunnel list                      # list your tunnels
cloudflared tunnel info <TUNNEL-NAME>        # show tunnel details + active connections
```

## Removing or replacing a tunnel

```bash
cloudflared tunnel delete <TUNNEL-NAME>
```

This removes the tunnel from Cloudflare but does not delete the DNS record — clean that up in the dashboard, or replace it with `cloudflared tunnel route dns` pointing at a different tunnel.