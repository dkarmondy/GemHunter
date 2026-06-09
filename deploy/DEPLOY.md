# Deploying GemHunter to the Raspberry Pi 5

Goal: run the scout 24/7, writing the dataset to the external SSD, viewable from your phone.

## 1. Get the code on the Pi
```bash
cd ~
git clone <your-repo-url> GemHunter      # or copy the folder over with scp/rsync
cd GemHunter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Secrets & config
```bash
cp .env.example .env          # paste your eBay (and Pushover) keys
cp config.example.yaml config.yaml   # your searches + min_score
```

## 3. Point the database at the SSD (not the SD card)
SD cards wear out under constant writes; the SSD is where the comps dataset lives.
```bash
# find the SSD and make a folder for the db (adjust to your mount)
lsblk
sudo mkdir -p /mnt/ssd/gemhunter && sudo chown pi:pi /mnt/ssd/gemhunter
```
Make sure the SSD auto-mounts on boot (add it to `/etc/fstab`).

## 4. Install the service
```bash
sudo cp deploy/gemhunter.service /etc/systemd/system/
# edit the file if your username/paths differ (User=, WorkingDirectory=, ExecStart=)
sudo systemctl daemon-reload
sudo systemctl enable --now gemhunter
```

Check it:
```bash
systemctl status gemhunter        # should be "active (running)"
journalctl -u gemhunter -f        # live logs: watch the [cycle] lines
```

## 5. View the gems from your phone (same network)
The loop refreshes `gems.html` every cycle. Serve it:
```bash
# simple: a one-off static server (or make a second systemd unit)
cd ~/GemHunter && python3 -m http.server 8080
```
Then browse to `http://<pi-ip>:8080/gems.html` from your phone.
(Pushover alerts also reach your phone anywhere once keys are set.)

## Updating later
```bash
cd ~/GemHunter && git pull && sudo systemctl restart gemhunter
```

## Notes
- **The dataset accumulates automatically.** Every cycle upserts each listing into the
  `listings` table on the SSD — gems and rejects, with score/mode/price/seller/timestamps.
  This is the seed of the comps dataset; Phase 3 adds auction snapshots + sold outcomes.
- **Back it up.** The history is irreplaceable — copy `gemhunter.db*` to a second disk or
  cloud on a schedule (a cron job is fine; the db is tiny).
- Toggle `--enrich` in the service file for richer (size/movement) scoring.
