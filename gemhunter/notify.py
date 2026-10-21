"""Push notifications to the phone via Pushover. Falls back to console in dry-run."""

from __future__ import annotations

import requests

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


class Notifier:
    def __init__(self, user_key: str = "", api_token: str = ""):
        self.user_key = user_key
        self.api_token = api_token

    @property
    def live(self) -> bool:
        return bool(self.user_key and self.api_token)

    def send_digest(self, title: str, message: str, url: str = "") -> None:
        """One notification summarising many listings."""
        self._dispatch(title, message, url)

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
