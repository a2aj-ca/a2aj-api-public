# Arbutus Cloud VM Setup

Runbook for launching a VM on the Digital Research Alliance of Canada's Arbutus cloud (OpenStack/Horizon), bootstrapping SSH access via a temporary public IP, and locking it down behind Tailscale so the VM has no public SSH exposure.

Assumes a fresh project where networks and routers have been pre-created by DRAC (the default).

Up to date as of April 28, 2026.

## Flavor naming

- `p` = persistent (Ceph-backed disk, durable) — use for services
- `c` / `cb` / `cm` = compute (local disk, faster but tied to hypervisor) — use for batch jobs

Format: `pN-MMgb` where `N` = vCPUs, `MM` = GB RAM. All `p` flavors have a 20 GB root disk; for more storage, boot from a Cinder volume.

For this project (2026) used: `p8-16gb` for api and 'p16-24gb' used for elastic search.

## Pre-launch (one-time per workstation)

### 1. Generate an SSH key on the workstation you'll SSH from

Windows PowerShell:

```powershell
ssh-keygen -t ed25519 -C "<descriptive-comment>"
Get-Content "$HOME\.ssh\id_ed25519.pub" | Set-Clipboard
```

Linux/macOS:

```bash
ssh-keygen -t ed25519 -C "<descriptive-comment>"
cat ~/.ssh/id_ed25519.pub
```

### 2. Import the public key in Horizon

**Compute → Key Pairs → Import Public Key**, paste the `.pub` contents.

### 3. Create a bootstrap SSH security group

**Network → Security Groups → + Create Security Group** (name e.g. `ssh-bootstrap`), then **Manage Rules → + Add Rule**:

- Rule: SSH
- Remote: CIDR
- CIDR: `<your-current-public-ip>/32`

### 4. Allocate a floating IP

**Network → Floating IPs → Allocate IP To Project** with pool `Public-Network`.

## Launch the VM

**Compute → Instances → Launch Instance**:

| Tab | Setting |
|---|---|
| Details | Instance name, count = 1 |
| Source | Boot Source: **Image**; Create New Volume: **Yes**; Volume Size: 500 GB; Image: Ubuntu (latest stable) |
| Flavor | A `p` flavor matching the workload |
| Networks | The project's private network (e.g. `def-<project>-prod-network`) |
| Security Groups | **`default`** + **`ssh-bootstrap`** |
| Key Pair | The imported key |

Skip the remaining tabs. Click **Launch Instance** and wait for state = Active.

## Post-launch

### 1. Associate the floating IP

**Compute → Instances → [VM] → Actions → Associate Floating IP**.

### 2. SSH in

```bash
ssh ubuntu@<floating-ip>
```

The default user on Ubuntu cloud images is `ubuntu`. There is no console password — SSH key is the only way in. (The noVNC console is useful for viewing boot logs, not for login.)

### 3. Update and install Tailscale

Before running with `--advertise-tags`, define the tag in your tailnet ACL policy under `tagOwners` (admin console → Access Controls). Otherwise the command will fail with "tags are invalid or not permitted."

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --advertise-tags=tag:<your-tag> --ssh
```

The `--ssh` flag enables Tailscale-native SSH (in addition to standard OpenSSH).

Tailscale auto-starts on reboot via systemd (`tailscaled.service`). Tagged devices have key expiry disabled by default, so they reconnect automatically with no manual re-auth after kernel updates, power loss, or hypervisor restarts.

### 4. Lock it down

Confirm SSH works via the tailnet IP (or MagicDNS hostname) from your workstation:

```bash
ssh ubuntu@<tailscale-ip>
```

Then in Horizon:

- **Compute → Instances → [VM] → Actions → Disassociate Floating IP**
- **Compute → Instances → [VM] → Actions → Edit Security Groups** — remove `ssh-bootstrap`, leave only `default`

The VM now has zero public exposure. Tailscale and any future tunneled services (e.g. Cloudflare Tunnel) work outbound-only and don't require an inbound IP or open ports.

## Gotchas

- **Don't delete the default security group's egress rules** — cloud-init relies on outbound to the metadata service.
- **`p` flavor disk is 20 GB unless you boot from volume.** Verify the actual root disk size with `lsblk` after launch.
- **Public IP rotation**: residential ISP IPs change; if `ssh-bootstrap` is still attached and your IP changes, update the rule. Tailscale removes this concern entirely.
- **Define Tailscale tags in the ACL policy first.** `--advertise-tags` will fail if the tag isn't declared in `tagOwners`.