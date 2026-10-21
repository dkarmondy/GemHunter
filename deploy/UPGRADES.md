# Pi upgrade runbook: Tailscale + National Rarities digest

Two independent upgrades. Each section is copy-paste-able on the Pi.

## 1. Tailscale (private access from anywhere)

```bash
# Pi
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# prints a login URL — open it, sign in (Google/Apple/GitHub), done
tailscale ip -4   # note the 100.x.y.z address
```

iPhone: install the **Tailscale** app from the App Store, sign in with the SAME
account, toggle the VPN on. Then from anywhere:

    http://<pi-tailscale-ip>:8080        (the GemHunter app)

Optional polish:
- In the Tailscale admin console, enable **MagicDNS** → use `http://raspberrypi:8080`.
- Disable key expiry for the Pi (admin console → machine → "Disable key expiry")
  so it never logs itself out.

Nothing is exposed publicly: both devices make outbound connections only, and only
devices on your tailnet can reach the Pi.

## 2. National Rarities new-drop digest

One push notification when `nationalrarities` lists watches you have not been
told about yet, leading with IWC and Breitling Navitimer. Quiet by default: a
run that finds nothing new sends nothing.

**Pushover keys first** — without them the digest only prints to the journal.
Create an application at <https://pushover.net>, then on the Pi add to
`~/Projects/GemHunter/.env`:

```
PUSHOVER_USER_KEY=...
PUSHOVER_API_TOKEN=...
```

Install the timer:

```bash
cd ~/Projects/GemHunter && git pull
sudo cp deploy/gemhunter-rarities.service deploy/gemhunter-rarities.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gemhunter-rarities.timer
```

The first run seeds the whole current consignment (a few hundred lots) and
sends nothing — it has no way to know what you have already browsed. Every run
after that reports only genuine arrivals.

```bash
systemctl list-timers gemhunter-rarities        # when it next fires
journalctl -u gemhunter-rarities -n 30          # what the last run found
.venv/bin/python -m gemhunter.rarities \
    --db data/gemhunter.db --dry-run   # print, don't push
```

Runs twice a day (09:00 and 18:00, jittered). To change that, edit
`OnCalendar` in the timer and `sudo systemctl daemon-reload`.
