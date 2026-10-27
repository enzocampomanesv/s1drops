# Always-on deployment

Runs s1drops on a small cloud VM so it stays up without your laptop. Two
containers, managed by [`compose.yaml`](compose.yaml):

```
browser ──HTTPS──► Cloudflare ◄──outbound tunnel── cloudflared ──► app:8765
                  (your hostname)                   (VM, no open ports)   │
                                                                          ▼
                                                              /srv/s1drops/cache  (cubes)
                                                              /srv/s1drops/lcz    (optional)
```

- **app**: the existing `s1drops/Dockerfile` image. It restarts on crash or
  reboot, has a healthcheck, and its port is bound to `127.0.0.1` only.
- **tunnel**: a Cloudflare *named* tunnel. Unlike the quick tunnel in
  `run_s1drops_demo.bat`, it keeps the same hostname across restarts. The VM
  opens no inbound ports except SSH, and TLS is handled by Cloudflare.

## 1. Pick a VM

| | Minimum | Comfortable |
| --- | --- | --- |
| vCPU | 2 | 4 |
| RAM | 4 GB (lower `S1DROPS_MAX_CUBE_BYTES` to ~1 GB) | 8 GB |
| Disk | 30 GB | 100 GB+ if you bake multi-year or 10 m cubes |
| OS | Ubuntu 22.04 / 24.04 LTS | |

RAM is the binding constraint: `write_cube()` loads a whole cube into memory, and
the app refuses bakes estimated above `S1DROPS_MAX_CUBE_BYTES` (2 GB default).
Any provider works (Hetzner, DigitalOcean, Lightsail, GCE, Azure). The data
comes from Planetary Computer, which is hosted in Azure **West Europe**, so a VM
in or near Western Europe makes bakes noticeably faster.

## 2. Cloudflare tunnel (one-time, in the browser)

Needs a free Cloudflare account and a domain whose DNS is on Cloudflare.

1. Zero Trust dashboard → **Networks → Tunnels → Create a tunnel** →
   *Cloudflared*. Name it `s1drops`.
2. On the install screen, copy the token (the long string after `--token`). Skip
   the install commands, because compose runs cloudflared for you.
3. **Public hostname** tab → add `s1drops.<your-domain>` with service
   `HTTP` → `app:8765`. (`app` is the compose service name, which resolves
   inside the compose network.)

No domain yet? You can still test on the VM with a quick tunnel (see
[Troubleshooting](#troubleshooting)), but its URL changes on every restart.

## 3. Set up the VM

```bash
ssh you@vm
curl -fsSL https://raw.githubusercontent.com/enzocampomanesv/s1drops/v2/deploy/bootstrap-vm.sh | bash
# private repo? clone it yourself (deploy key or token), then: bash deploy/bootstrap-vm.sh
```

The script installs Docker, enables the firewall with only SSH open, creates
`/srv/s1drops/{cache,lcz}`, clones the repo to `/opt/s1drops`, and copies
`.env.example` to `.env` (mode 600). Log out and back in so the docker group
takes effect, then:

```bash
nano /opt/s1drops/deploy/.env       # admin password + CLOUDFLARE_TUNNEL_TOKEN
cd /opt/s1drops/deploy
docker compose up -d --build        # first build takes a few minutes
docker compose ps                   # app should show (healthy), tunnel Up
```

Open `https://s1drops.<your-domain>`.

### Optional: LCZ filtering

```bash
# from your laptop (~1.4 GB)
scp lcz_cog.tif you@vm:/srv/s1drops/lcz/
# on the VM: uncomment S1DROPS_LCZ_PATH in .env, then
docker compose up -d
```

LCZ is read at bake time, so only cubes baked after this step get the layer.

### Optional: move existing cubes from the laptop

Cubes are plain directories, so copy them and rebuild the registry:

```bash
rsync -a --progress /path/to/cache/ you@vm:/srv/s1drops/cache/
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
| Reach the app bypassing Cloudflare | from your laptop: `ssh -L 8765:127.0.0.1:8765 you@vm`, then open `http://localhost:8765` |

**Before redeploying or restarting, check the Admin tab's bake queue is empty.**
The queue lives in memory, so a restart drops queued and running jobs. Finished
cubes are on disk and unaffected.

## 5. Backups

Only `/srv/s1drops/cache` holds state. All of it can be rebuilt from Planetary
Computer, but a long bake is slow, so take a provider disk snapshot weekly, or:

```bash
tar -C /srv/s1drops -czf s1drops-cache-$(date +%F).tgz cache
```

If you lose `registry.json`, run `rescan` to rebuild it from the zarr stores.

## Security notes

- The Admin tab sits on the same public URL as Explore, protected only by
  `S1DROPS_ADMIN_PASSWORD`. Nothing limits login attempts, so use a long random
  password (`openssl rand -base64 24`).
- If the audience is a known team rather than the public, put the whole hostname
  behind **Cloudflare Access** (Zero Trust → Access → Applications →
  Self-hosted, policy *emails ending in …* or a list, one-time PIN login). It is
  free for up to 50 users, needs no code changes, and stops anonymous users from
  reaching the app at all, so they can't guess the admin password either.
- Docker bypasses `ufw` for published ports. The app's port is published on
  `127.0.0.1` only, so it is not exposed; keep it that way.
- `deploy/.env` is excluded from git and from the Docker build context.

## Troubleshooting

- **`tunnel` keeps restarting**: the token is wrong, or the tunnel was deleted
  in the dashboard. Check `docker compose logs tunnel`.
- **502 from Cloudflare**: the public hostname's service must be `http://app:8765`,
  not `localhost`. Inside the tunnel container, `localhost` is the tunnel itself.
- **App killed mid-bake (`docker compose ps` shows a restart, `dmesg` shows OOM)**:
  lower `S1DROPS_MAX_CUBE_BYTES`, or move to a bigger VM and raise
  `S1DROPS_MEM_LIMIT`.
- **Test without a domain**: `docker compose up -d app`, then
  `docker run --rm --network s1drops_default cloudflare/cloudflared tunnel --url http://app:8765`
  prints a temporary `trycloudflare.com` URL.
