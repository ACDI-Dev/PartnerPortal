import os
import json
import requests
from datetime import datetime, timezone
from extensions import cache
from integrations.gcs_client import get_gcs_client, RESELLER_ACCOUNTS_BLOB, BUCKET_NAME

API_KEY = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
ZOHO_RESELLER_API_URL = f"https://www.zohoapis.com/crm/v7/functions/vfreselleraccounts/actions/execute?auth_type=apikey&zapikey={API_KEY}&email=@acd-inc.com"

@cache.memoize(timeout=500)
def get_cached_reward_requests(api_email):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/perks_reward_requests/actions/execute?auth_type=apikey&zapikey={API_KEY}&email={api_email}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_rep_accounts(api_email):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfreselleraccounts/actions/execute?auth_type=apikey&zapikey={API_KEY}&email={api_email}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_licenses(api_email, account_id=None):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfresellerlicenselookup/actions/execute?auth_type=apikey&zapikey={API_KEY}&email={api_email}"
    if account_id:
        api_url += f"&account_id={account_id}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_claims(api_email):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/perks_claims_list/actions/execute?auth_type=apikey&zapikey={API_KEY}&email={api_email}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=300) 
def get_cached_rewards(api_email):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/pointslookup/actions/execute?auth_type=apikey&zapikey={API_KEY}&email={api_email}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400)
def get_cached_account_contacts(account_id):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfcontactlist/actions/execute?auth_type=apikey&zapikey={API_KEY}&account_id={account_id}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=1400)
def get_cached_shipment_tracking(account_id):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/shipmenttrackingdetails/actions/execute?auth_type=apikey&zapikey={API_KEY}&Account_ID={account_id or ''}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_open_deals(api_email):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vf_open_deals/actions/execute?auth_type=apikey&zapikey={API_KEY}&email={api_email}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400)
def get_cached_open_installs(api_email):
    api_url = f"https://www.zohoapis.com/crm/v7/functions/getopenpostsalebyrep/actions/execute?auth_type=apikey&zapikey={API_KEY}&repemail={api_email}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

def refresh_reseller_accounts_gcs():
    print("Reseller cache expired or missing. Fetching fresh data from Zoho API...", flush=True)
    response = requests.get(ZOHO_RESELLER_API_URL, timeout=30)
    response.raise_for_status()
    data = response.json()

    if data.get("code") == "success":
        raw_output = data.get("details", {}).get("output", "{}")
        parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output

        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(RESELLER_ACCOUNTS_BLOB)

        blob.upload_from_string(json.dumps(parsed_data, indent=2), content_type='application/json')
        print("Successfully refreshed reseller_accounts.json in GCS.", flush=True)
        return parsed_data
    else:
        raise RuntimeError(f"Zoho API error: {data}")

def get_or_update_reseller_accounts():
    try:
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(RESELLER_ACCOUNTS_BLOB)

        if blob.exists():
            blob.reload()
            last_updated = blob.updated
            now = datetime.now(timezone.utc)

            if (now - last_updated).total_seconds() < 86400:
                print("Serving reseller accounts from GCS cache...", flush=True)
                return json.loads(blob.download_as_string())

        return refresh_reseller_accounts_gcs()

    except Exception as e:
        print(f"Error accessing GCS cache or Zoho API: {e}", flush=True)
        if 'blob' in locals() and blob.exists():
            return json.loads(blob.download_as_string())
        return {}