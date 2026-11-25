# Always-on deployment

Runs the s1drops API on a cloud VM: the back end of Space4All's Demolition
detection page, <https://space4all.web.app/tools/demolitions/>, where people
sign in with their Space4All account. The default route costs nothing: an
**Oracle Cloud Always Free** Arm VM, published with **Tailscale Funnel**.

```
space4all.web.app ──HTTPS──► Tailscale Funnel ◄──outbound── tailscaled (host) ──► 127.0.0.1:8766 ──► api
(the page)                   <name>.<tailnet>.ts.net         (VM, no open ports)                       │
                                                                                                       ▼
                                                                                 /srv/s1drops/cache  (areas, photos)
                                                                                 /srv/s1drops/db     (accounts, verdicts)
                                                                                 /srv/s1drops/lcz    (optional)
```

- **api** ([`compose.yaml`](compose.yaml)): the `s1drops/Dockerfile` image
  running `s1drops.api.main`. It restarts on crash or reboot, has a
  healthcheck, and its port is bound to `127.0.0.1` only. It answers `/api/…`
  and forwards every other path to the Space4All page (old links included).
- **Tailscale Funnel** runs on the host and gives a fixed HTTPS hostname with no
  domain, no TLS setup and no inbound ports. It is free on Tailscale's personal
  plan.
- Have your own domain on Cloudflare? See the
  [Cloudflare tunnel alternative](#alternative-cloudflare-named-tunnel).

Until 25 November 2026 the VM also ran the Solara app, the tool's first
interface with its own sign-in. It was retired once the Space4All page covered
all of it; its address now forwards there.

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
(CPU, network and memory all low) for 7 days. To avoid it, upgrade the account
to **Pay As You Go** (Billing → Upgrade). Always Free resources stay free and
reclamation no longer applies, but anything you create *outside* the free
allowance would be billed, so set a budget alert at $1 (Billing → Budgets).

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
nano /opt/s1drops/deploy/.env     # S1DROPS_ADMIN_EMAILS=you@example.org (the first admin)
cd /opt/s1drops/deploy
docker compose up -d --build      # first build takes several minutes
docker compose ps                 # api should show (healthy)
```

## 3. Publish it with Tailscale Funnel

1. Create a free account at <https://tailscale.com> (Google, GitHub or Microsoft
   login).
2. On the VM:

   ```bash
   sudo tailscale up --hostname s1drops    # open the printed link to log in
   sudo tailscale funnel --bg 8766
   curl https://<your-funnel-name>.ts.net/api/health      # {"ok":true}
   ```

   The first `funnel` run prints a link to enable HTTPS and Funnel for your
   tailnet. Open it, approve, and run the command again. It prints the public
   URL. `--bg` makes the setting persist across reboots.
3. Optional: in the Tailscale admin console (DNS), swap the tailnet name for a
   random "fun name" so the URL doesn't reveal your account.
4. The page reads the API's address from the Space4All site's build setting
   `PUBLIC_DEMOLITIONS_API_URL` (default: this VM's Funnel address).

`tailscale funnel status` shows what's published; `sudo tailscale funnel --https=443 off`
unpublishes it.

The API accepts Space4All sign-ins only (`S1DROPS_FIREBASE_PROJECT`), from the
Space4All site only (`S1DROPS_API_ORIGINS`), and shows the tool to approved
users only.

### Optional: LCZ filtering

```bash
# from your laptop (~1.4 GB)
scp lcz_cog.tif ubuntu@<vm-public-ip>:/srv/s1drops/lcz/
# on the VM: set S1DROPS_LCZ_PATH=/data/lcz/lcz_cog.tif in .env, then
docker compose up -d
```

LCZ is read at bake time, so only areas baked after this step get the layer.

### Optional: move existing areas from the laptop

Areas are plain directories, so copy them and rebuild the registry:

```bash
rsync -a --progress /path/to/cache/ ubuntu@<vm-public-ip>:/srv/s1drops/cache/
docker compose exec api python -m s1drops.cli rescan --cache-dir /data/cache
```

## 4. Day-to-day

| Task | Command (in `/opt/s1drops/deploy`) |
| --- | --- |
| Deploy new code | `bash update.sh` (pull, rebuild, restart) |
| Logs | `docker compose logs -f api` (errors only: there is no request log) |
| Restart | `docker compose restart api` |
| Stop everything | `docker compose down` (areas on disk are kept) |
| CLI | `docker compose exec api python -m s1drops.cli list --cache-dir /data/cache` |

**Before redeploying or restarting, check the bake queue is empty** (the
Space4All page → Admin → Areas). The queue lives in memory, so a restart drops
queued and running jobs. Finished areas are on disk and unaffected.

### Monthly updates

Every area gets the newest Sentinel-1 passes once a month: from the 1st, after
02:00 UTC, the API queues an update job in the bake queue. Admin → Areas →
**Update now** queues the same job at any time (it then counts as that month's).
The first automatic run is on the 1st of the month *after* the API first
starts, never straight away.

Each update writes the extended area as a new store, checks it (same grid, all
old passes plus the new ones, real values), and only then switches to it. The
previous version moves to `/srv/s1drops/cache/previous/` (one per area) and stays
until the next update succeeds. A failed or empty update leaves the area as it
was. Runs and their results are listed under Admin → Areas and in
`/srv/s1drops/cache/updates.json`.

To roll an area back by hand (with the API stopped: `docker compose stop api`):

```bash
cd /srv/s1drops/cache
mv Lagos_100m_2017-01-01_2026-11-03_nm.zarr /tmp/            # the update to undo
mv previous/Lagos_100m_2017-01-01_2026-10-01_nm.zarr .
rm registry.json                                             # rebuilt on next start
docker compose start api
```

Shared links name an area without its dates, so they keep working after updates.

## 5. Backups

**The database (`/srv/s1drops/db/app.db`: accounts, roles, verdicts, reference
data, feedback) cannot be rebuilt: back it up.** `backup-db.sh` copies it safely
while the API runs and keeps the last 30 days in `/srv/s1drops/db-backups`.
Install it once:

```bash
(crontab -l 2>/dev/null; echo "15 3 * * * bash /opt/s1drops/deploy/backup-db.sh") | crontab -
bash /opt/s1drops/deploy/backup-db.sh && ls /srv/s1drops/db-backups   # test: lists app-<date>.db
```

Keep a copy off the VM too, e.g. from your laptop:
`scp ubuntu@<vm-public-ip>:/srv/s1drops/db-backups/app-$(date +%F).db .`
The file holds names, emails and verdicts: store it somewhere private.

The area cache (`/srv/s1drops/cache`) can all be rebuilt from Planetary
Computer, but a long bake is slow. Oracle's Always Free tier includes 5 volume
backups (Block Storage → Boot Volumes → Create backup), or archive the cache:

```bash
tar -C /srv/s1drops -czf s1drops-cache-$(date +%F).tgz cache
```

If you lose `registry.json`, run `rescan` to rebuild it from the zarr stores.

## 6. Accounts

People sign in with their Space4All account (verified email) on the page and
ask for access; the tool's admins approve them under Admin → Users.

| Role | Can |
| --- | --- |
| Waiting for approval | Sign in; nothing else until an admin sets a role |
| Validator | Use the tool, record verdicts and see everyone's |
| Reviewer | Validator, plus veto any verdict and download all verdicts |
| Admin | Reviewer, plus manage users, prepare and delete areas, import reference data, read feedback |

- **Approve someone in advance** (Admin → Users): their email and role; they
  get it the first time they sign in with that email, once verified.
- Emails in `S1DROPS_ADMIN_EMAILS` are admins from their first sign-in.
- Switching someone to **Disabled** shuts them out at once. Their verdicts stay
  on record.
- Roles are matched to Space4All accounts by verified email, then by the
  account itself.

### Reference demolitions

Admin → Reference data: a GeoJSON of known demolition polygons (lon/lat) with a
year field (`demo_year` by default). Only admins ever see them: a "ref" mark in
Strongest changes and a reference check under the list. They are never drawn on
the map, shown to other roles or exported.

## Security notes

- Funnel makes the API **public**, but every route except `/api/health` needs a
  Space4All sign-in, and everything but asking for access needs an approved
  role. Calls are accepted from the Space4All site only (CORS).
- There is no request log, so visitors' IP addresses are not kept (Space4All's
  privacy notice says so). Errors are still logged.
- Docker bypasses host firewalls for published ports. The API's port is
  published on `127.0.0.1` only, so it is not exposed; keep it that way.
- `deploy/.env` is excluded from git and from the Docker build context.

## Troubleshooting

- **API killed mid-bake (`docker compose ps` shows a restart, `dmesg` shows OOM)**:
  lower `S1DROPS_MAX_CUBE_BYTES`, or raise `S1DROPS_API_MEM_LIMIT` if the VM has room.
- **Funnel URL doesn't load**: `tailscale funnel status` should list port 8766.
  Check the API answers locally with `curl http://127.0.0.1:8766/api/health`.
- **Instance stopped by itself**: likely idle reclamation, see step 1. Start it
  from the console; containers and Funnel come back on their own.

---

## Alternative: Cloudflare named tunnel

Use this instead of Funnel if you have a domain whose DNS is on Cloudflare.

1. Zero Trust dashboard → **Networks → Tunnels → Create a tunnel** →
   *Cloudflared*, named `s1drops`. Copy the token (the string after `--token`)
   and skip the install commands.
2. **Public hostname**: `s1drops.<your-domain>` → `HTTP` → `api:8766`. Use
   `api`, the compose service name, not `localhost`: inside the tunnel
   container, `localhost` is the tunnel itself.
3. In `deploy/.env`, uncomment `COMPOSE_PROFILES=cloudflare` and set
   `CLOUDFLARE_TUNNEL_TOKEN`, then `docker compose up -d`. This starts the
   `tunnel` container next to the API.
4. Set the Space4All site's `PUBLIC_DEMOLITIONS_API_URL` to that hostname.

If the `tunnel` container keeps restarting, the token is wrong or the tunnel was
deleted in the dashboard (`docker compose logs tunnel`).
