"""Shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Listing:
    item_id: str
    title: str
    price: float
    currency: str
    buying_option: str          # "FIXED_PRICE" or "AUCTION"
    url: str
    shipping_cost: float = 0.0
    shipping_known: bool = False
    import_charges: float = 0.0
    import_charges_known: bool = False
    condition: str = ""
    search_name: str = ""       # which of your searches surfaced it

    # --- extended fields from the search response (cheap) ---
    seller_username: str = ""
    seller_feedback_pct: float = 0.0
    seller_feedback_score: int = 0
    item_location: str = ""
    country: str = ""           # ISO code from itemLocation, e.g. US / JP / GB
    image_url: str = ""
    bid_count: int = 0

    # --- enriched from getItem (only for scored candidates) ---
    aspects: dict = field(default_factory=dict)   # {"Case Size": "40 mm", ...}
    auth_guarantee: bool = False

    @property
    def is_auction(self) -> bool:
        return self.buying_option == "AUCTION"
