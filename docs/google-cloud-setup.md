# Running the bots on Google Cloud's always-free VM

**Why:** Render's free tier routes every service in a region through an
outbound address shared with every other customer there ("Outbound IP
ranges are shared across *all* services in the same region" — Render's own
docs). Discord refused that address in Oregon, Singapore and Ohio in turn.
None of it followed from this bot's traffic, and no code change lifts it.

A VM gives you an address nobody else shares. If Discord ever refuses
*that* one, you can delete the instance and get another — which is the
thing Render could never do: four containers in a row came back on the
same banned address.

## What "always free" actually covers

Google's free `e2-micro` is permanent, not a trial, but it is narrow:

| | Limit | Consequence if you miss it |
|---|---|---|
| Region | `us-west1`, `us-central1`, `us-east1` **only** | Any other region bills at full price |
| Machine type | `e2-micro` | Anything larger bills |
| Disk | 30 GB **standard** persistent disk | An SSD disk bills, even at 10 GB |
| Egress | 1 GB/month from North America | Overage bills per GB |

The US-only restriction costs you nothing here. Your players talk to
Discord, not to this bot, so the only latency that matters is bot-to-
Discord — and Discord is behind Cloudflare's anycast either way.

Egress is the one to keep an eye on for the first month. Gateway traffic
is mostly *inbound* (free) and this bot's outbound is heartbeats plus
small REST calls, so 1 GB should be ample — but it is a real cap, and
nothing warns you before it bills.

## 1. Create the instance

Compute Engine → **Create instance**.

- **Region:** `us-central1` (Iowa). Any of the three free regions works.
- **Machine type:** `e2-micro` — under "E2", not the default.
- **Boot disk:** Ubuntu 24.04 LTS, **Balanced or Standard**, 10–30 GB.
  Change this: the default is often an SSD, which bills.
- **Firewall:** leave both HTTP boxes unchecked. The bots only make
  outbound connections, and the keep-alive port stays closed — nothing
  external needs it any more.

## 2. Provision

Connect with the **SSH** button in the console, or `gcloud compute ssh`.

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/nariokeith/fb-timer-bot.git /tmp/fb
sudo bash /tmp/fb/deploy/setup.sh
```

`setup.sh` installs Python 3.13 from deadsnakes, clones to
`/opt/fb-timer-bot`, builds the venv, installs the pins and enables the
systemd unit. It is idempotent — re-running it is safe.

**Python 3.13 specifically, not the distro's python3.** `requirements.txt`
pins `audioop-lts`, which declares `requires-python >= 3.13` (audioop left
the standard library in 3.13; this is the backport discord.py needs).
Ubuntu 24.04 ships 3.12, so the distro interpreter cannot install these
pins at all.

**On the service user.** Oracle's Ubuntu images create a user called
`ubuntu`; Google's create one named after your Google account instead. The
unit ships with a `__APP_USER__` placeholder and `setup.sh` substitutes
whoever ran it, so this needs no thought — but it is why you must run the
script with `sudo` from your own SSH session rather than as root directly.

### Swap, on 1 GB of RAM

The three bots measure at about 210 MB together, so they fit — but `pip`
resolving the pins can spike well past that, and an `e2-micro` has no swap
by default. Cheap insurance:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Do this *before* `setup.sh` if the pip step gets killed.

## 3. Credentials

The script stops before starting, because a credential-less start exits 78
and looks like a crash rather than a remaining step.

```bash
sudo nano /opt/fb-timer-bot/.env
sudo chmod 600 /opt/fb-timer-bot/.env
```

Copy the values out of Render → Environment:

```
DISCORD_TOKEN=...
ATTENDANCE_DISCORD_TOKEN=...
ITEMS_DISCORD_TOKEN=...
SHEET_ID=...
ITEMS_SHEET_ID=...
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
GEMINI_API_KEY=...
BOT_TZ=Asia/Manila
ITEMS_GEAR_DAILY_CAP=3
```

`GOOGLE_SERVICE_ACCOUNT_JSON` must be the entire key on **one line**. A
pasted multi-line key is not valid JSON and fails with "Service account
JSON is not valid JSON" — this cost an hour on 2026-08-18.

## 4. Cut over

Suspend the Render service **first**. Two live copies double-post every
boss warning and both write the same pinned state messages.

```bash
sudo systemctl start fb-timer-bot
journalctl -u fb-timer-bot -f
```

Success looks like:

```
[supervisor] keep-alive listening on port 8080
[supervisor] starting timer: /opt/fb-timer-bot/.venv/bin/python -u bot.py
Logged in as M2 TIMER#9367
[items] logged in as Ukay-Ukay sa Bahay ni Talong#6513
Attendance bot logged in as BK Attendance#8249
[items] restored state from remembered #item-distribution
State restored from Discord: True
```

No data migration. All three bots keep their state in pinned Discord
messages, so the queue, the timers and any raffle session restore on the
first successful login.

Once you are happy, delete the Render service and the HetrixTools monitor.

## Day-to-day

```bash
sudo bash /opt/fb-timer-bot/deploy/update.sh   # after pushing to main
journalctl -u fb-timer-bot -f                  # follow
journalctl -u fb-timer-bot --since "1 hour ago" | grep -i "rate"
sudo systemctl restart fb-timer-bot
```

There is no auto-deploy. Pushing to `main` changes nothing until you run
`update.sh` — a deliberate trade: the 2026-08-18 outage was made worse by
an auto-deploy landing mid-rate-limit and restarting all three bots into
it.

## What this drops

Everything that exists only to survive a platform that sleeps:

| Piece                        | On Render | Here |
|------------------------------|-----------|------|
| Spin-down after 15 min idle  | yes       | never |
| keep-alive HTTP server       | required  | vestigial (firewalled) |
| HetrixTools / UptimeRobot    | required  | not needed |
| DuckDNS domain               | required  | not needed |
| 750 instance-hours/month cap | yes       | none |

## What this does not fix

Discord treats datacentre address ranges more harshly than residential
ones, so a dedicated VM address is *likely* but not *certain* to end the
blocks. The only environment with a perfect record on 2026-08-18 was a
home machine.

If Google's address is refused too, delete the instance and create another
— you get a new IP, which is the option Render never offered. If that also
fails, a machine at home is the fallback with actual evidence behind it.
