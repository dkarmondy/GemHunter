# Pi upgrade runbook: comps + visual taste + Tailscale

Three independent upgrades. Each section is copy-paste-able on the Pi.

## 1. Comps (personal sold/purchase data)

The comps CSV is built on the Windows box (personal data, not in git):

```powershell
# Windows, repo root — already done once; rerun after updating Watch History.xlsx
python scripts/import_comps.py --xlsx "E:\WATCHES\Watch History.xlsx" `
    --pdf "E:\WATCHES\COLLECTION\IWCFliegerChronograph3706\eBay Item Bid History.pdf" `
    --to-csv data/comps.csv
# copy to the Pi
scp data\comps.csv dkarm@raspberrypi.local:/home/dkarm/Projects/GemHunter/data/
```

Then on the Pi:
```bash
cd ~/Projects/GemHunter && git pull
.venv/bin/python scripts/import_comps.py --from-csv data/comps.csv \
    --db /mnt/ssd/gemhunter/gemhunter.db
```

## 2. Visual taste scoring (contrastive CLIP)

Scores each listing thumbnail by similarity-to-your-collection MINUS similarity-to-junk.
- **Negatives** (generic) ship in the repo — `git pull` brings them, nothing to copy.
- **Positives** = your collection anchors, built on Windows from `E:\WATCHES\COLLECTION`
  and copied over (personal; gitignored):

```powershell
# Windows
scp data\taste_anchors.npz dkarm@raspberrypi.local:/home/dkarm/Projects/GemHunter/data/
```

```bash
# Pi
cd ~/Projects/GemHunter && git pull
.venv/bin/pip install -r requirements-visual.txt     # onnxruntime/pillow/numpy (aarch64)
.venv/bin/python -c "from gemhunter import visual; visual.download_model()"   # ~350MB, one time
sed -i 's/^visual: false/visual: true/' config.yaml
sudo systemctl restart gemhunter
journalctl -u gemhunter -f     # look for e.g. "looks+2.4" / "looks-2" in reasons
```

Notes: bonus is capped at +3 / penalty -2 — a ranking nudge, never a gate. Adds a CLIP
embed per candidate (~1s each on the Pi 5; fine on a 30-min cycle). If model/anchors/deps
are missing the scorer skips it silently. Rebuild positives anytime collection grows:
`python scripts/build_taste_anchors.py --collection "E:\WATCHES\COLLECTION"`.

## 3. Tailscale (private access from anywhere)

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
