"""Gem detection. Phase 1 rule: price at or under your max for that search.

Auctions are included on their *current* price for now. The Phase 4 forecaster
will replace this with a predicted-final-price comparison for auctions.
"""

from __future__ import annotations

from .config import Search
from .models import Listing


def is_gem(listing: Listing, search: Search) -> bool:
    return listing.price <= search.max_price
