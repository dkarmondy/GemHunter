# Deploying GemHunter to the Raspberry Pi 5

Goal: serve the web app (Listing lookup + National Rarities) 24/7 and run the
Rarities digest twice a day, with the database on the Pi's own storage.

Two units:
- `gemhunter-web.service` — the web app on port 8080.
- `gemhunter-rarities.service` + `.timer` — the twice-daily new-drop push.

## 1. Get the code on the Pi
```bash
mkdir -p ~/Projects && cd ~/Projects
git clone <your-repo-url> GemHunter      # or copy the folder over with scp/rsync
cd GemHunter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Secrets & config
```bash
cp .env.example .env                 # paste your eBay (and Pushover) keys
cp config.example.yaml config.yaml   # marketplace + your ZIP for shipping estimates
```

## 3. Database location
The db lives at `~/Projects/GemHunter/data/gemhunter.db` on the Pi's own storage
(gitignored). It holds one small table — which Rarities lots have been seen,
and your hearts / thumbs-downs — written only when you vote and when the digest
runs, so an SD card handles it without any wear concern.
```bash
mkdir -p ~/Projects/GemHunter/data
```

## 4. Install the services
```bash
sudo cp deploy/gemhunter-web.service deploy/gemhunter-rarities.service \
        deploy/gemhunter-rarities.timer /etc/systemd/system/
# edit the files if your username/paths differ (User=, WorkingDirectory=, ExecStart=)
sudo systemctl daemon-reload
sudo systemctl enable --now gemhunter-web gemhunter-rarities.timer
```

Check it:
```bash
systemctl status gemhunter-web                  # should be "active (running)"
systemctl list-timers gemhunter-rarities        # when the digest next fires
```

Then browse to `http://<pi-ip>:8080` from your phone (or over Tailscale — see
`UPGRADES.md`).

## Updating later
```bash
cd ~/Projects/GemHunter && git pull && sudo systemctl restart gemhunter-web
```

## Migrating a Pi that ran the old scout off an external SSD
One time. Stop everything first so the database's WAL is flushed into the file.
```bash
sudo systemctl stop gemhunter gemhunter-web gemhunter-rarities.timer
sudo systemctl disable gemhunter
sudo rm /etc/systemd/system/gemhunter.service
cd ~/Projects/GemHunter && git pull
mkdir -p data && cp -av /mnt/ssd/gemhunter/gemhunter.db* data/
```
Then do step 4 to install the updated unit files and start them. Your Rarities
votes and seen-lots history come across in the copy; the old scout tables ride
along unused and do no harm.

Once the app shows your old hearts, remove the SSD's line from `/etc/fstab`
before unplugging it, or the Pi may hang at boot waiting for the missing drive.
The SSD's copy is your backup until then.

## Notes
- **Back it up.** Your Rarities votes live only in `data/gemhunter.db`, and SD
  cards fail more often than SSDs. Copy it off the Pi now and then.
