import os
import json
from google.cloud import storage

BUCKET_NAME = 'acdifiles'
BLOB_NAME = 'ID/employees.json'
RESELLER_ACCOUNTS_BLOB = 'CACHE/reseller_accounts.json'

def get_gcs_client():
    """Initializes and returns the authenticated GCS client for both Cloud Run and Local."""
    secret_manager_path = '/secrets/acdifiles.json'
    local_key_path = 'acdifiles.json'

    if os.path.exists(secret_manager_path):
        return storage.Client.from_service_account_json(secret_manager_path)
    
    if os.path.exists(local_key_path):
        return storage.Client.from_service_account_json(local_key_path)

    return storage.Client()

def load_employee_data():
    """Fetches employee data securely via the GCS SDK."""
    try:
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(BLOB_NAME)
        
        json_data = blob.download_as_string()
        return json.loads(json_data)
    except Exception as e:
        print(f"Error fetching from GCS: {e}")
        return []