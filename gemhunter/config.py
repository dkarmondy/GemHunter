"""Load configuration from config.yaml and credentials from .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class Search:
    name: str
    query: str
    max_price: float = 0.0                       # 0 => no price cap
    condition_ids: list = field(default_factory=lambda: [3000, 7000])  # used + for-parts
    buying_options: list = field(default_factory=lambda: ["AUCTION", "FIXED_PRICE"])
    category_ids: str = "31387"                  # Wristwatches


@dataclass
class Config:
    poll_interval_seconds: int
    marketplace: str
    searches: list[Search]
    buyer_country: str = ""
    buyer_postal_code: str = ""
    min_score: float = 4.0          # only alert candidates scoring >= this
    alert_limit: int = 25           # max alerts per cycle
    enrich: bool = False            # call getItem on candidates for size/movement/etc.
    visual: bool = False            # CLIP visual-taste bonus (needs anchors + model)
    # Credentials (empty string => that integration runs in dry-run mode)
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    pushover_user_key: str = ""
    pushover_api_token: str = ""

    @property
    def has_ebay_keys(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)

    @property
    def has_pushover_keys(self) -> bool:
        return bool(self.pushover_user_key and self.pushover_api_token)


def load_config(config_path: str | Path = "config.yaml") -> Config:
    load_dotenv()  # pulls .env into os.environ if present

    path = Path(config_path)
    if not path.exists():
        example = path.with_name("config.example.yaml")
        path = example if example.exists() else path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    searches = [Search(**s) for s in raw.get("searches", [])]

    return Config(
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 1800)),
        marketplace=raw.get("marketplace", "EBAY_US"),
        searches=searches,
        buyer_country=os.getenv("GEMHUNTER_BUYER_COUNTRY", raw.get("buyer_country", "")),
        buyer_postal_code=os.getenv("GEMHUNTER_BUYER_POSTAL_CODE", raw.get("buyer_postal_code", "")),
        min_score=float(raw.get("min_score", 4.0)),
        alert_limit=int(raw.get("alert_limit", 25)),
        enrich=bool(raw.get("enrich", False)),
        visual=bool(raw.get("visual", False)),
        ebay_client_id=os.getenv("EBAY_CLIENT_ID", ""),
        ebay_client_secret=os.getenv("EBAY_CLIENT_SECRET", ""),
        pushover_user_key=os.getenv("PUSHOVER_USER_KEY", ""),
        pushover_api_token=os.getenv("PUSHOVER_API_TOKEN", ""),
    )
