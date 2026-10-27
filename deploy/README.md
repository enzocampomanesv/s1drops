# Always-on deployment

Runs s1drops on a cloud VM so it stays up without your laptop. The default route
costs nothing: an **Oracle Cloud Always Free** Arm VM, published with
**Tailscale Funnel**.

```
browser ──HTTPS──► Tailscale Funnel ◄──outbound── tailscaled (host) ──► 127.0.0.1:8765 ──► app
                  s1drops.<tailnet>.ts.net         (VM, no open ports)                       │
                                                                                            ▼
                                                                         /srv/s1drops/cache  (cubes)
                                                                         /srv/s1drops/lcz    (optional)
```

- **app** ([`compose.yaml`](compose.yaml)): the `s1drops/Dockerfile` image. It
  restarts on crash or reboot, has a healthcheck, and its port is bound to
  `127.0.0.1` only.
- **Tailscale Funnel** runs on the host and gives a fixed HTTPS hostname with no
  domain, no TLS setup and no inbound ports. It is free on Tailscale's personal
  plan.
- Have your own domain on Cloudflare? See the
  [Cloudflare tunnel alternative](#alternative-cloudflare-named-tunnel).

## 1. Create the VM (Oracle Cloud Always Free)

1. Sign up at <https://www.oracle.com/cloud/free/>. A card is needed for
   identity verification only; Always Free resources are not charged.
   **Your home region is permanent**, and Always Free compute only runs there.
   Pick **Germany Central (Frankfurt)** or **Netherlands Northwest (Amsterdam)**:
   the satellite data lives in Azure West Europe (Netherlands), so nearby means
   faster bakes.
2. Compute → Instances → **Create instance**:
   - Image: **Canonical Ubuntu 24.04** (the regular image, not *Minimal*).
   - Shape: **Ampere → VM.Standard.A1.Flex**, **4 OCPU, 24 GB** RAM (the whole
     Always Free allowance).
   - Boot volume: up to **200 GB** (also within Always Free).
   - Upload your SSH public key. The login user is `ubuntu`.
3. "Out of host capacity" is common for A1 in busy regions. Retry later, or try
   another availability domain in the same region.

**Idle reclamation.** Oracle may stop Always Free instances that stay idle
(CPU, network and memory all low) for 7 days, and an app that is mostly waiting
for clicks can look idle. To avoid it, upgrade the account to **Pay As You Go**
(Billing → Upgrade). Always Free resources stay free and reclamation no longer
applies, but anything you create *outside* the free allowance would be billed,
so set a budget alert at $1 (Billing → Budgets).

## 2. Set up the VM

```bash
ssh ubuntu@<vm-public-ip>
curl -fsSL https://raw.githubusercontent.com/enzocampomanesv/s1drops/v2/deploy/bootstrap-vm.sh | bash
exit && ssh ubuntu@<vm-public-ip>       # log back in so the docker group applies
```

The script installs Docker and Tailscale, creates `/srv/s1drops/{cache,lcz}`,
clones the repo to `/opt/s1drops`, and copies `.env.example` to `.env`
(mode 600). On Oracle images it leaves the firewall alone: Oracle's built-in
rules already allow only SSH.

```bash
nano /opt/s1drops/deploy/.env     # S1DROPS_ADMIN_PASSWORD: openssl rand -base64 24
                                  # with 24 GB RAM, also set S1DROPS_MEM_LIMIT=20g
cd /opt/s1drops/deploy
docker compose up -d --build      # first build takes several minutes
docker compose ps                 # app should show (healthy)
```

## 3. Publish it with Tailscale Funnel

1. Create a free account at <https://tailscale.com> (Google, GitHub or Microsoft
   login).
2. On the VM:

   ```bash
   sudo tailscale up --hostname s1drops    # open the printed link to log in
   sudo tailscale funnel --bg 8765
   ```

   The first `funnel` run prints a link to enable HTTPS and Funnel for your
   tailnet. Open it, approve, and run the command again. It prints the public
   URL, `https://s1drops.<tailnet>.ts.net`. `--bg` makes the setting persist
   across reboots.
3. Optional: in the Tailscale admin console (DNS), swap the tailnet name for a
   random "fun name" so the URL doesn't reveal your account.

`tailscale funnel status` shows what's published; `sudo tailscale funnel --https=443 off`
unpublishes it.

### Optional: LCZ filtering

```bash
# from your laptop (~1.4 GB)
scp lcz_cog.tif ubuntu@<vm-public-ip>:/srv/s1drops/lcz/
# on the VM: uncomment S1DROPS_LCZ_PATH in .env, then
docker compose up -d
```

LCZ is read at bake time, so only cubes baked after this step get the layer.

### Optional: move existing cubes from the laptop

Cubes are plain directories, so copy them and rebuild the registry:

```bash
rsync -a --progress /path/to/cache/ ubuntu@<vm-public-ip>:/srv/s1drops/cache/
docker compose exec app python -m s1drops.cli rescan --cache-dir /data/cache
```

## 4. Day-to-day

| Task | Command (in `/opt/s1drops/deploy`) |
| --- | --- |
| Deploy new code | `bash update.sh` (pull, rebuild, restart) |
| Logs | `docker compose logs -f app` |
| Restart | `docker compose restart app` |
| Stop everything | `docker compose down` (cubes on disk are kept) |
| CLI | `docker compose exec app python -m s1drops.cli list --cache-dir /data/cache` |
| Reach the app bypassing Funnel | from your laptop: `ssh -L 8765:127.0.0.1:8765 ubuntu@<vm-public-ip>`, then open `http://localhost:8765` |

**Before redeploying or restarting, check the Admin tab's bake queue is empty.**
The queue lives in memory, so a restart drops queued and running jobs. Finished
cubes are on disk and unaffected.

## 5. Backups

Only `/srv/s1drops/cache` holds state. All of it can be rebuilt from Planetary
Computer, but a long bake is slow. Oracle's Always Free tier includes 5 volume
backups (Block Storage → Boot Volumes → Create backup), or archive the cache:

```bash
tar -C /srv/s1drops -czf s1drops-cache-$(date +%F).tgz cache
```

If you lose `registry.json`, run `rescan` to rebuild it from the zarr stores.

## Security notes

- Funnel makes the app **public**. The Admin tab sits on the same URL as
  Explore, protected only by `S1DROPS_ADMIN_PASSWORD`, and nothing limits login
  attempts, so use a long random password.
- For a known team only, use `sudo tailscale serve --bg 8765` instead of
  `funnel`. The app is then reachable only by people you invite to your tailnet
  (free for up to 3 users; each installs Tailscale on their device).
- Docker bypasses host firewalls for published ports. The app's port is
  published on `127.0.0.1` only, so it is not exposed; keep it that way.
- `deploy/.env` is excluded from git and from the Docker build context.

## Troubleshooting

- **App killed mid-bake (`docker compose ps` shows a restart, `dmesg` shows OOM)**:
  lower `S1DROPS_MAX_CUBE_BYTES`, or raise `S1DROPS_MEM_LIMIT` if the VM has room.
- **Funnel URL doesn't load**: `tailscale funnel status` should list port 8765.
  Check the app answers locally with `curl -I http://127.0.0.1:8765`.
- **Instance stopped by itself**: likely idle reclamation, see step 1. Start it
  from the console; containers and Funnel come back on their own.

---

## Alternative: Cloudflare named tunnel

Use this instead of Funnel if you have a domain whose DNS is on Cloudflare, or if
you want Cloudflare Access (email one-time-PIN login in front of the whole app,
free up to 50 users).

1. Zero Trust dashboard → **Networks → Tunnels → Create a tunnel** →
   *Cloudflared*, named `s1drops`. Copy the token (the string after `--token`)
   and skip the install commands.
2. **Public hostname**: `s1drops.<your-domain>` → `HTTP` → `app:8765`. Use
   `app`, the compose service name, not `localhost`: inside the tunnel
   container, `localhost` is the tunnel itself.
3. In `deploy/.env`, uncomment `COMPOSE_PROFILES=cloudflare` and set
   `CLOUDFLARE_TUNNEL_TOKEN`, then `docker compose up -d`. This starts the
   `tunnel` container next to the app.

If the `tunnel` container keeps restarting, the token is wrong or the tunnel was
deleted in the dashboard (`docker compose logs tunnel`).
