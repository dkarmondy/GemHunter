"""Build the NEGATIVE visual anchor set — watches the user would never buy.

These come from generic eBay searches (not personal photos), so the result is safe
to commit to the repo. The scorer compares each listing to (positives - negatives):
high only when it looks like his collection AND unlike this junk.

Usage:
  python scripts/build_negative_anchors.py --out gemhunter/taste_negatives.npz
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NEGATIVE_QUERIES = [
    "apple watch", "samsung galaxy watch", "smartwatch", "fitbit",
    "invicta watch men", "fashion quartz watch", "g-shock", "fossil watch",
    "michael kors watch", "digital watch men", "skmei watch",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="gemhunter/taste_negatives.npz")
    ap.add_argument("--per", type=int, default=6)
    args = ap.parse_args()

    import numpy as np
    import requests
    import onnxruntime as ort
    from gemhunter import visual
    from gemhunter.config import load_config, Search
    from gemhunter.ebay import EbayClient

    visual.download_model()
    visual._session = ort.InferenceSession(str(visual.MODEL_PATH),
                                           providers=["CPUExecutionProvider"])
    cfg = load_config("config.yaml")
    c = EbayClient(cfg.ebay_client_id, cfg.ebay_client_secret, cfg.marketplace)

    embs = []
    for q in NEGATIVE_QUERIES:
        s = Search(name="neg", query=q, max_price=0, condition_ids=[3000],
                   buying_options=["FIXED_PRICE", "AUCTION"])
        got = 0
        for it in c.search(s):
            if got >= args.per or not it.image_url:
                continue
            try:
                embs.append(visual.embed_image(requests.get(it.image_url, timeout=15).content))
                got += 1
            except Exception:
                pass
        print(f"  {q}: {got}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, embeddings=np.stack(embs))
    print(f"wrote {out} — {len(embs)} negative anchors")


if __name__ == "__main__":
    main()
