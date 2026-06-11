"""Build the visual taste anchor set from the user's watch-collection photos.

Walks a collection directory (one folder per watch, containing the seller's listing
photos and the user's own shots), embeds every watch photo with CLIP, and saves the
embeddings to data/taste_anchors.npz. Screenshot-style files (descriptions, seller
pages, bid histories) are excluded by name.

Usage:
  python scripts/build_taste_anchors.py --collection "E:\\WATCHES\\COLLECTION" \
      --out data/taste_anchors.npz
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemhunter import visual  # noqa: E402

EXCLUDE_NAMES = ("description", "seller", "sellernotes", "intro", "details",
                 "bids", "messages", "watchbox", "receipt", "invoice", "ebay",
                 "raffle", "screenshot")
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def collect_photos(root: Path) -> list[Path]:
    photos = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        if p.name.startswith("._"):                  # macOS sidecar junk
            continue
        stem = p.stem.lower()
        if any(x in stem for x in EXCLUDE_NAMES):
            continue
        photos.append(p)
    return photos


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", required=True)
    ap.add_argument("--out", default="data/taste_anchors.npz")
    args = ap.parse_args()

    import numpy as np

    visual.download_model()
    # load the session without requiring an existing anchors file
    import onnxruntime as ort
    visual._session = ort.InferenceSession(str(visual.MODEL_PATH),
                                           providers=["CPUExecutionProvider"])

    photos = collect_photos(Path(args.collection))
    print(f"embedding {len(photos)} photos from {args.collection} ...")
    embs, names, skipped = [], [], 0
    for i, p in enumerate(photos, 1):
        try:
            emb = visual.embed_image(p.read_bytes())
            embs.append(emb)
            names.append(f"{p.parent.name}/{p.name}")
        except Exception:
            skipped += 1
        if i % 25 == 0:
            print(f"  {i}/{len(photos)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, embeddings=np.stack(embs), names=np.array(names))
    print(f"wrote {out} — {len(embs)} anchors ({skipped} unreadable skipped)")


if __name__ == "__main__":
    main()
