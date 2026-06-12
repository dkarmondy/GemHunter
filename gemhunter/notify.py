"""Push alerts to the phone via Pushover. Falls back to console in dry-run."""

from __future__ import annotations

import requests

from .models import Listing

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def _money(n: float) -> str:
    return f"${n:,.0f}"


def _cost_note(listing: Listing) -> str:
    ship = float(getattr(listing, "shipping_cost", 0.0) or 0.0)
    ship_known = bool(getattr(listing, "shipping_known", False))
    imp = float(getattr(listing, "import_charges", 0.0) or 0.0)
    import_known = bool(getattr(listing, "import_charges_known", False))
    total = listing.price + ship + imp
    bits = []
    if ship_known:
        bits.append(f"ship {_money(ship)}" if ship else "ship free")
    if imp:
        bits.append(f"import {_money(imp)}")
    elif (listing.country or "").upper() and (listing.country or "").upper() != "US" and not import_known:
        bits.append("import TBD")
    if not bits:
        return ""
    return f"\nlanded est. {_money(total)} ({' · '.join(bits)})"


def _origin_note(listing: Listing) -> str:
    cc = (listing.country or "").upper()
    if not cc or cc == "US":
        return ""
    humid = {"JP", "SG", "MY", "ID", "PH", "TW", "VN", "IN", "TH", "HK", "BR"}
    moisture = " · moisture risk" if cc in humid else ""
    return f"\norigin {cc} · import fees{moisture}"


class Notifier:
    def __init__(self, user_key: str = "", api_token: str = ""):
        self.user_key = user_key
        self.api_token = api_token

    @property
    def live(self) -> bool:
        return bool(self.user_key and self.api_token)

    @staticmethod
    def _format(listing: Listing) -> tuple[str, str]:
        kind = "Auction" if listing.is_auction else "Buy It Now"
        title = f"Gem: {listing.search_name} — ${listing.price:,.0f}"
        message = f"{listing.title}\n{kind} · ${listing.price:,.0f} {listing.currency}{_cost_note(listing)}{_origin_note(listing)}"
        return title, message

    def send(self, listing: Listing) -> None:
        title, message = self._format(listing)
        self._dispatch(title, message, listing.url)

    def send_scored(self, result) -> None:
        """Alert from a Score: shows stream/mode, score, price, reasons, seller."""
        l = result.listing
        kind = "Auction" if l.is_auction else "BIN"
        bid = f" · {l.bid_count} bids" if l.is_auction and l.bid_count else ""
        title = f"{result.mode} — ${l.price:,.0f} ({kind}{bid})"
        seller = (f"\nseller {l.seller_feedback_pct:.0f}% ({l.seller_feedback_score})"
                  if l.seller_feedback_pct else "")
        message = (f"{l.title}\n"
                   f"score {result.score:.0f} · {', '.join(result.reasons)}"
                   f"{_cost_note(l)}{_origin_note(l)}{seller}")
        self._dispatch(title, message, l.url)

    def _dispatch(self, title: str, message: str, url: str) -> None:
        if not self.live:
            print(f"[ALERT] {title}\n    {message}\n    {url}")
            return
        requests.post(
            PUSHOVER_URL,
            data={
                "token": self.api_token,
                "user": self.user_key,
                "title": title,
                "message": message,
                "url": url,
                "url_title": "View on eBay",
            },
            timeout=30,
        ).raise_for_status()
