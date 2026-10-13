import os
import time
import requests
from flask import current_app

ZOHO_ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com")

class ZohoAuthManager:
    """Manages Zoho Desk OAuth tokens and automatically refreshes them when expired."""
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token = None
        self._token_expires_at = 0

    def get_access_token(self) -> str:
        if not self._access_token or time.time() >= (self._token_expires_at - 60):
            self._refresh_access_token()
        return self._access_token

    def _refresh_access_token(self):
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
        self._token_expires_at = time.time() + data.get("expires_in", 3600)

zoho_auth = ZohoAuthManager(
    client_id=os.environ.get("ZOHO_CLIENT_ID", ""),
    client_secret=os.environ.get("ZOHO_CLIENT_SECRET", ""),
    refresh_token=os.environ.get("ZOHO_REFRESH_TOKEN", "")
)

def vf_create_ticket(
    ticket_subject: str,
    firstname: str,
    lastname: str,
    email: str,
    description: str,
    product_involved: str,
    crn: str,
    reseller_account_name: str = "",
) -> str:
    url = "https://desk.zoho.com/api/v1/tickets"

    try:
        token = zoho_auth.get_access_token()
    except Exception as ex:
        current_app.logger.error(f"Failed to fetch Zoho access token: {ex}")
        return "0"

    headers = {
        "orgId": os.environ.get("ZOHO_ORG_ID", "7868846"),
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json",
    }

    formatted_description = description.replace("ZZZZZ", "").replace("\n", "<br>")

    custom_fields = {
        "cf_product": product_involved,
        "cf_crn": crn,
    }
    
    if reseller_account_name:
        custom_fields["cf_reseller_account_name"] = reseller_account_name

    payload = {
        "subject": ticket_subject,
        "departmentId": "20427000000006907",
        "contact": {
            "firstName": firstname,
            "lastName": lastname,
            "email": email,
        },
        "description": formatted_description,
        "status": "Open",
        "priority": "Sev 3",
        "channel": "AI Chat Bot",
        "email": email,
        "cf": custom_fields,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        return str(data.get("ticketNumber", "0"))
    except Exception as ex:
        current_app.logger.error(f"Error creating ticket in Zoho Desk: {ex}")
        return "0"