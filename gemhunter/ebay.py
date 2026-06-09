"""eBay Browse API client, plus a sample provider for dry-run mode.

Both expose the same method:  search(search) -> list[Listing]
so the rest of the app doesn't care which one it's talking to.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import requests

from .config import Search
from .models import Listing

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
SCOPE = "https://api.ebay.com/oauth/api_scope"
WRISTWATCH_CATEGORY = "31387"


class EbayClient:
    """Real client against the eBay Browse API (active listings only)."""

    def __init__(self, client_id: str, client_secret: str, marketplace: str = "EBAY_US"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self._token = ""
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        creds = f"{self.client_id}:{self.client_secret}".encode()
        headers = {
            "Authorization": f"Basic {base64.b64encode(creds).decode()}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials", "scope": SCOPE}
        resp = requests.post(OAUTH_URL, headers=headers, data=data, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    @staticmethod
    def _build_filter(search: Search) -> str:
        parts = ["buyingOptions:{" + "|".join(search.buying_options) + "}"]
        if getattr(search, "condition_ids", None):
            ids = "|".join(str(c) for c in search.condition_ids)
            parts.append("conditionIds:{" + ids + "}")
        if search.max_price and search.max_price > 0:
            parts.append(f"price:[..{search.max_price}]")
            parts.append("priceCurrency:USD")
        return ",".join(parts)

    def search(self, search: Search) -> list[Listing]:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
        }
        params = {
            "q": search.query,
            "filter": self._build_filter(search),
            "sort": "newlyListed",
            "limit": 50,
        }
        category = getattr(search, "category_ids", "") or WRISTWATCH_CATEGORY
        if category:
            params["category_ids"] = category
        resp = requests.get(BROWSE_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return self._parse(resp.json(), search.name)

    def get_item(self, item_id: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
        }
        resp = requests.get(ITEM_URL + item_id, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def enrich(self, listing: Listing) -> Listing:
        """Pull item specifics (size, movement, box/papers, …) for a candidate."""
        try:
            d = self.get_item(listing.item_id)
        except Exception:
            return listing
        listing.aspects = {a.get("name"): a.get("value")
                           for a in d.get("localizedAspects", []) if a.get("name")}
        progs = d.get("qualifiedPrograms", []) or []
        listing.auth_guarantee = bool(d.get("authenticityGuarantee")) or \
            ("AUTHENTICITY_GUARANTEE" in progs)
        return listing

    @staticmethod
    def _parse(payload: dict, search_name: str) -> list[Listing]:
        listings = []
        for item in payload.get("itemSummaries", []):
            options = item.get("buyingOptions", ["FIXED_PRICE"])
            is_auction = "AUCTION" in options
            # Auctions carry the live bid in currentBidPrice; BIN uses price.
            money = (item.get("currentBidPrice") if is_auction else item.get("price")) \
                or item.get("price") or {}
            seller = item.get("seller", {}) or {}
            try:
                pct = float(seller.get("feedbackPercentage") or 0)
            except (TypeError, ValueError):
                pct = 0.0
            loc = item.get("itemLocation", {}) or {}
            listings.append(
                Listing(
                    item_id=item.get("itemId", ""),
                    title=item.get("title", ""),
                    price=float(money.get("value", 0) or 0),
                    currency=money.get("currency", "USD"),
                    buying_option="AUCTION" if is_auction else "FIXED_PRICE",
                    url=item.get("itemWebUrl", ""),
                    condition=item.get("condition", ""),
                    search_name=search_name,
                    seller_username=seller.get("username", ""),
                    seller_feedback_pct=pct,
                    seller_feedback_score=int(seller.get("feedbackScore") or 0),
                    item_location=", ".join(
                        x for x in [loc.get("city"), loc.get("country")] if x),
                    image_url=(item.get("image", {}) or {}).get("imageUrl", ""),
                    bid_count=int(item.get("bidCount") or 0),
                )
            )
        return listings


class SampleEbayClient:
    """Offline stand-in: returns canned listings so the pipeline runs without keys."""

    def __init__(self, sample_path: str | Path | None = None):
        if sample_path is None:
            sample_path = Path(__file__).parent / "sample_listings.json"
        self.sample_path = Path(sample_path)

    def search(self, search: Search) -> list[Listing]:
        data = json.loads(self.sample_path.read_text(encoding="utf-8"))
        items = data.get(search.name, [])
        return [Listing(search_name=search.name, **item) for item in items]

    def enrich(self, listing: Listing) -> Listing:  # no aspects in dry-run
        return listing
