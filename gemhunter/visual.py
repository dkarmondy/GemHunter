"""Visual taste score: CLIP image embeddings vs. an anchor set of the user's watches.

No training involved. scripts/build_taste_anchors.py embeds the user's collection
photos once into taste_anchors.npz; at runtime each candidate listing's thumbnail is
embedded and compared (cosine similarity to the nearest anchors). The similarity is
mapped to a small score bonus — a ranking nudge, never a gate.

Heavy deps (onnxruntime/PIL/numpy) and the model file are optional: if anything is
missing, is_available() returns False and the scorer silently skips the bonus.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

MODEL_URL = ("https://huggingface.co/Qdrant/clip-ViT-B-32-vision/"
             "resolve/main/model.onnx")
MODEL_PATH = Path(os.getenv("GEMHUNTER_CLIP_MODEL",
                            str(Path.home() / ".cache/gemhunter/clip-vit-b32.onnx")))
ANCHORS_PATH = Path(os.getenv("GEMHUNTER_ANCHORS", "data/taste_anchors.npz"))

# CLIP preprocessing constants
_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)

_session = None
_anchors = None
_failed = False


def download_model(dest: Path = MODEL_PATH) -> Path:
    import urllib.request
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        print(f"[visual] downloading CLIP model -> {dest} (~350MB, one time)")
        urllib.request.urlretrieve(MODEL_URL, dest)
    return dest


def _load():
    global _session, _anchors, _failed
    if _failed or _session is not None:
        return
    try:
        import numpy as np
        import onnxruntime as ort
        if not MODEL_PATH.exists() or not ANCHORS_PATH.exists():
            _failed = True
            return
        _session = ort.InferenceSession(str(MODEL_PATH),
                                        providers=["CPUExecutionProvider"])
        data = np.load(ANCHORS_PATH)
        emb = data["embeddings"].astype("float32")
        emb /= (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-8)
        _anchors = emb
    except Exception as exc:
        print(f"[visual] disabled: {exc}")
        _failed = True


def is_available() -> bool:
    _load()
    return _session is not None and _anchors is not None


def embed_image(data: bytes):
    """bytes -> normalized 512-d embedding (numpy array)."""
    import numpy as np
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    # resize shortest side to 224, center-crop 224x224
    w, h = img.size
    scale = 224 / min(w, h)
    img = img.resize((round(w * scale), round(h * scale)), Image.BICUBIC)
    w, h = img.size
    left, top = (w - 224) // 2, (h - 224) // 2
    img = img.crop((left, top, left + 224, top + 224))
    arr = np.asarray(img, dtype="float32") / 255.0
    arr = (arr - _MEAN) / _STD
    arr = arr.transpose(2, 0, 1)[None].astype("float32")
    name = _session.get_inputs()[0].name
    out = _session.run(None, {name: arr})[0][0].astype("float32")
    return out / (np.linalg.norm(out) + 1e-8)


def similarity_from_url(url: str, timeout: int = 20) -> float | None:
    """Fetch a listing thumbnail and return taste similarity in [0,1], or None."""
    if not is_available() or not url:
        return None
    try:
        import numpy as np
        import requests
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        emb = embed_image(resp.content)
        sims = _anchors @ emb
        k = min(5, len(sims))
        return float(np.sort(sims)[-k:].mean())   # mean of top-5 nearest anchors
    except Exception:
        return None


def bonus(sim: float | None, max_bonus: float = 3.0) -> float:
    """Map similarity to a score bonus. <0.55 ~ unlike his watches -> 0;
    0.75+ ~ strongly his taste -> full bonus."""
    if sim is None:
        return 0.0
    lo, hi = 0.55, 0.75
    if sim <= lo:
        return 0.0
    return round(max_bonus * min(1.0, (sim - lo) / (hi - lo)), 1)
