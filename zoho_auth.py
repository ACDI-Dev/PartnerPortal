import time
import requests

# Zoho Data Center URL (US default: https://accounts.zoho.com)
# Options: accounts.zoho.com, accounts.zoho.eu, accounts.zoho.in, etc.
ZOHO_ACCOUNTS_URL = "https://accounts.zoho.com"


class ZohoAuthManager:

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token = None
        self._token_expires_at = 0  # Epoch timestamp in seconds

    def get_access_token(self) -> str:
        """Returns a valid access token.

        Refreshes the token if expired or within 60s of expiring.
        """
        # Refresh 60 seconds before actual expiry to prevent edge-case failures
        if not self._access_token or time.time() >= (
            self._token_expires_at - 60
        ):
            self._
            _token()

        return self._access_token

    def _refresh_access_token(self):
        """Requests a new access token from Zoho Accounts endpoint."""
        url = f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token"

        payload = {
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }

        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()

        data = response.json()

        if "error" in data:
            raise RuntimeError(f"Zoho Token Refresh Failed: {data['error']}")

        self._access_token = data["access_token"]
        # expires_in is in seconds (usually 3600)
        self._token_expires_at = time.time() + data.get("expires_in", 3600)