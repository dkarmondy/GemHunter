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
    active: bool = True
    item_end_date: str = ""
    inactive_reason: str = ""
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
    # eBay's numeric condition (3000 = pre-owned, 7000 = for parts). The AG
    # programme is switched on from this, never from the description.
    condition_id: int | None = None
    # True/False from the seller's prose, or None when it couldn't be read —
    # see authguard.description_text for why None is not False.
    description_indicates_as_is: bool | None = None
    auth_arbitrage_class: str = ""
    as_is_terms: list = field(default_factory=list)

    @property
    def is_auction(self) -> bool:
        return self.buying_option == "AUCTION"
