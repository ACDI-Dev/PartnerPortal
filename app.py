import os
import base64
import json
import requests
import time
import jwt
import io
import traceback
from functools import wraps
from flask import Flask, render_template, session, redirect, request, url_for, jsonify, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from urllib.parse import urlencode
import urllib.request
from datetime import date, timedelta, datetime, timezone
from flask_caching import Cache
from google.cloud import storage
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

app = Flask(__name__)

# Tell Flask to trust Google Cloud Run's X-Forwarded-* headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

# A secret key is required to manage Flask sessions locally and in production
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a-very-secure-local-secret')

# --- CACHING SETUP ---
redis_url = os.environ.get('REDIS_URL')

if redis_url:
    app.config['CACHE_TYPE'] = 'RedisCache'
    app.config['CACHE_REDIS_URL'] = redis_url
else:
    app.config['CACHE_TYPE'] = 'SimpleCache'

app.config['CACHE_DEFAULT_TIMEOUT'] = 14400 # 4 hours

# Initialize the cache
cache = Cache(app)

# --- ZOHO DESK OAUTH & API INTEGRATION ---
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
        # Refreshes token if it's missing or within 60 seconds of expiration
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

# Instantiate Zoho Auth Manager from environment variables
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
    """Replicates standalone.VFCreateTicket Deluge logic using Python REST API."""
    url = "https://desk.zoho.com/api/v1/tickets"

    try:
        token = zoho_auth.get_access_token()
    except Exception as ex:
        app.logger.error(f"Failed to fetch Zoho access token: {ex}")
        return "0"

    headers = {
        "orgId": os.environ.get("ZOHO_ORG_ID", "7868846"),
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json",
    }

    formatted_description = description.replace("ZZZZZ", "").replace(
        "\n", "<br>"
    )

    # Base custom fields
    custom_fields = {
        "cf_product": product_involved,
        "cf_crn": crn,
    }
    
    # Add reseller account name if populated
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
        response = requests.post(
            url, headers=headers, json=payload, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("ticketNumber", "0"))
    except Exception as ex:
        app.logger.error(f"Error creating ticket in Zoho Desk: {ex}")
        return "0"

# Initialize OAuth
oauth = OAuth(app)

# Register FusionAuth as the OIDC provider
fusionauth = oauth.register(
    name='fusionauth',
    client_id=os.environ.get('FUSIONAUTH_CLIENT_ID'),
    client_secret=os.environ.get('FUSIONAUTH_CLIENT_SECRET'),
    server_metadata_url=f"{os.environ.get('FUSIONAUTH_URL')}/.well-known/openid-configuration",
    client_kwargs={
        'scope': 'openid profile email'
    }
)

# Map FusionAuth Application IDs to Human-Readable Names
AUTHORIZED_APP_MAP = {
    '10ec4e31-10a5-417a-92e3-24b886e1c750': 'ACDI Reseller Portal',
    '27318a59-d4ae-47a5-b300-3eebabae6aba': 'ACE',
    '2c6ebaaa-4419-4b3c-8fff-1770f7122810': 'Partner Perks',
    '3c219e58-ed0e-4b18-ad48-f4f92793ae32': 'FusionAuth',
    '416edefe-23fd-48f1-9355-4f63f6965711': 'Quote Portal',
    '51f6ec5d-b5bc-4cd4-9c39-7a3ac364588f': 'Tenant manager',
    'ca7f9e18-d00d-40f7-bc31-89012984199a': 'Zoho CRM Portal',
    '2e4da041-ed7f-478e-a01b-753c0a414f7a': 'Zoho Desk'
}

# --- GCS Configuration ---
BUCKET_NAME = 'acdifiles'
BLOB_NAME = 'ID/employees.json'

def get_gcs_client():
    """Initializes and returns the authenticated GCS client for both Cloud Run and Local."""
    secret_manager_path = '/secrets/acdifiles.json'
    local_key_path = 'acdifiles.json'

    # 1. Cloud Run Secret Manager Mount
    if os.path.exists(secret_manager_path):
        return storage.Client.from_service_account_json(secret_manager_path)
    
    # 2. Local File in Project Root
    if os.path.exists(local_key_path):
        print("test")
        return storage.Client.from_service_account_json(local_key_path)

    # 3. Default Cloud Run Service Account Credentials
    return storage.Client()

def get_user_identity():
    return session.get('user')

def is_token_valid(token):
    if not token:
        print("Token validation failed: No token provided in session.", flush=True)
        return False
        
    cache_key = f"token_valid:{token}"
    cached_result = cache.get(cache_key)
    
    if cached_result is not None:
        return cached_result

    print("Executing FusionAuth API validation (Cache Miss)", flush=True)
        
    fusionauth_url = os.environ.get('FUSIONAUTH_URL')
    if not fusionauth_url:
        print("Token validation failed: FUSIONAUTH_URL environment variable is missing.", flush=True)
        return False
        
    validate_url = f"{fusionauth_url}/api/jwt/validate"
    headers = {"Authorization": f"Bearer {token}"}
    
    print(f"Validating token starting with: {token[:15]}...", flush=True)
    
    try:
        response = requests.get(validate_url, headers=headers)
        
        if response.status_code == 200:
            try:
                # Decode claims from the token
                unverified_claims = jwt.decode(token, options={"verify_signature": False})
                
                # --- NEW AUTHORIZATION CHECK ---
                REQUIRED_APP_ID = '10ec4e31-10a5-417a-92e3-24b886e1c750'
                if unverified_claims.get("applicationId") != REQUIRED_APP_ID:
                    print(f"Token validation failed: User is not authorized for app {REQUIRED_APP_ID}.", flush=True)
                    cache.set(cache_key, False, timeout=60)
                    return False
                
                print("FusionAuth validation successful (200 OK) and applicationId matches.", flush=True)
                
                exp_timestamp = unverified_claims.get("exp")
                
                if exp_timestamp:
                    time_to_live = int(exp_timestamp) - int(time.time())
                    cache_timeout = max(0, time_to_live)
                else:
                    cache_timeout = 300 
                    
            except Exception as e:
                print(f"Failed to decode token for claims extraction: {e}", flush=True)
                cache_timeout = 300 
                
            cache.set(cache_key, True, timeout=cache_timeout)
            return True
        else:
            print(f"FusionAuth validation rejected the token. Status: {response.status_code}, Response: {response.text}", flush=True)
            cache.set(cache_key, False, timeout=60)
            return False
            
    except requests.RequestException as e:
        print(f"Network error during FusionAuth validation: {str(e)}", flush=True)
        return False

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user_identity()
        
        if not user:
            session['next'] = request.url
            return redirect(url_for('login'))
            
        token = user.get('token')
        
        if not is_token_valid(token):
            next_url = request.url
            session.clear()  
            session['next'] = next_url
            return redirect(url_for('login'))
            
        return f(user=user, *args, **kwargs)
    return decorated_function

# --- CACHED API HELPERS ---
@cache.memoize(timeout=500)
def get_cached_reward_requests(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/perks_reward_requests/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_rep_accounts(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfreselleraccounts/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_licenses(api_email, account_id=None):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfresellerlicenselookup/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    if account_id:
        api_url += f"&account_id={account_id}"
        
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_claims(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/perks_claims_list/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=300) 
def get_cached_rewards(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/pointslookup/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()


# --- CONTACTS LOOKUP HELPERS & ROUTE ---

@cache.memoize(timeout=14400)
def get_cached_account_contacts(account_id):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfcontactlist/actions/execute?auth_type=apikey&zapikey={api_key}&account_id={account_id}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@app.route('/rep/accounts/<account_id>/contacts')
@login_required
def account_contacts(user, account_id):
    account_name = request.args.get('account_name', 'Account')
    
    is_admin = 'Admin' in user.get('permissions', []) or 'original_admin_user' in session
    
    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_account_contacts, account_id)
            
        data = get_cached_account_contacts(account_id)

        if data.get("code") == "success":
            raw_output = data.get("details", {}).get("output", "{}")
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output

            if not isinstance(parsed_data, dict):
                parsed_data = {}

            contacts = parsed_data.get("Contacts") or parsed_data.get("data") or []
            total_contacts = parsed_data.get("Total_Number_of_Contacts", len(contacts))

            return render_template(
                'account_contacts.html', 
                user=user, 
                contacts=contacts, 
                total_contacts=total_contacts,
                account_id=account_id,
                account_name=account_name,
                is_admin=is_admin
            )
        else:
            return "API returned an error loading contacts.", 400

    except requests.RequestException as e:
        return f"Error fetching contacts data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing contacts data from the API.", 500


# --- SHIPMENT TRACKING HELPERS & ROUTE ---

@cache.memoize(timeout=1400)
def get_cached_shipment_tracking(account_id):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/shipmenttrackingdetails/actions/execute?auth_type=apikey&zapikey={api_key}&Account_ID={account_id or ''}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@app.route('/shipment-tracking')
@login_required
def shipment_tracking(user):
    account_id = session.get('active_account_id') or user.get('account_id', '')
    user_email = user.get('email', '')
    
    if user_email and '@acd-inc.com' in user_email.lower() and not account_id:
        account_id = '474481000000076079'

    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_shipment_tracking, account_id)

        data = get_cached_shipment_tracking(account_id)

        if data.get("code") == "success":
            raw_output = data.get("details", {}).get("output", "{}")
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output

            shipments = parsed_data.get("Shipments", [])
            total_shipments = parsed_data.get("Total_Number_of_Shipments", len(shipments))

            return render_template('shipment_tracking.html', user=user, shipments=shipments, total_shipments=total_shipments)
        else:
            return "API returned an error.", 400

    except requests.RequestException as e:
        return f"Error fetching shipment data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing shipment tracking data from API.", 500

@app.route('/rep/set_account/<account_id>')
@login_required
def set_active_account(user, account_id):
    session['active_account_id'] = account_id
    return redirect(url_for('reseller_licenses'))

@app.route('/login')
def login():
    redirect_uri = url_for('auth_callback', _external=True)
    return fusionauth.authorize_redirect(redirect_uri)

@app.route('/oauth-callback')
def auth_callback():
    token = fusionauth.authorize_access_token()
    user_info = token.get('userinfo')
    id_token = token.get('access_token')
    
    raw_app_ids = user_info.get('authorized_apps', [])
    
    REQUIRED_APP_ID = '10ec4e31-10a5-417a-92e3-24b886e1c750'
    if REQUIRED_APP_ID not in raw_app_ids:
        return "Unauthorized: You are not registered for the ACDI Reseller Portal.", 403
    
    friendly_apps = []
    for app_id in raw_app_ids:
        name = AUTHORIZED_APP_MAP.get(app_id, f'Unknown Application ({app_id})')
        friendly_apps.append(name)
    
    session['user'] = {
        'email': user_info.get('email'),
        'first_name': user_info.get('given_name', ''),
        'last_name': user_info.get('family_name', ''),
        'account': user_info.get('account', ''),
        'account_category': user_info.get('account_category', ''),
        'reseller_account': user_info.get('reseller_account', ''),
        'tier': user_info.get('tier', 'Standard'),
        'permissions': user_info.get('permissions', []),
        'authorized_apps': friendly_apps,
        'token': id_token
    }
    
    next_url = session.pop('next', url_for('dashboard'))
    return redirect(next_url)

@app.route('/logout')
def logout():
    session.clear()
    
    client_id = os.environ.get('FUSIONAUTH_CLIENT_ID')
    fusionauth_url = os.environ.get('FUSIONAUTH_URL')
    
    post_logout_redirect_uri = url_for('login', _external=True) 
    
    params = {
        'client_id': client_id,
        'post_logout_redirect_uri': post_logout_redirect_uri
    }
    logout_url = f"{fusionauth_url}/oauth2/logout?{urlencode(params)}"
    
    return redirect(logout_url)


# --- MAIN APPLICATION ROUTES ---


@app.route('/impersonate', methods=['POST'])
@login_required
def impersonate(user):
    is_admin = 'Admin' in user.get('permissions', []) or 'original_admin_user' in session
    if not is_admin:
        return jsonify({'status': 'error', 'message': 'Unauthorized. Admin access required.'}), 403

    data = request.get_json()
    target_email = data.get('email')

    if not target_email:
        return jsonify({'status': 'error', 'message': 'Email address is required.'}), 400

    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfloginas/actions/execute?auth_type=apikey&zapikey={api_key}&emailString={target_email}"

    try:
        response = requests.get(api_url)
        res_data = response.json()

        if 'original_admin_user' not in session:
            session['original_admin_user'] = user

        if res_data.get("code") == "success":
            raw_output = res_data.get("details", {}).get("output", "{}")
            user_messages = res_data.get("details", {}).get("userMessage", [])
            parsed_user = json.loads(raw_output) if isinstance(raw_output, str) else raw_output

            if not parsed_user or "No record found with that email address." in user_messages:
                session['user'] = session.pop('original_admin_user', user)
                warning_msg = user_messages[0] if user_messages else f"User record '{target_email}' could not be found."
                return jsonify({
                    'status': 'warning',
                    'message': f"{warning_msg} Admin session restored."
                }), 404

            target_account_id = parsed_user.get('account_id', '')

            impersonated_user = {
                'email': parsed_user.get('email'),
                'first_name': parsed_user.get('first_name', ''),
                'last_name': parsed_user.get('last_name', ''),
                'account': parsed_user.get('account', ''),
                'account_category': parsed_user.get('account_category', ''),
                'reseller_account': parsed_user.get('reseller_account', ''),
                'account_id': target_account_id,
                'tier': parsed_user.get('tier', 'Standard'),
                'permissions': parsed_user.get('permissions', []),
                'authorized_apps': parsed_user.get('authorized_apps', []),
                'token': user.get('token') 
            }

            session['user'] = impersonated_user
            
            if target_account_id:
                session['active_account_id'] = target_account_id
            else:
                session.pop('active_account_id', None)

            return jsonify({'status': 'success', 'message': f'Now impersonating {target_email}'}), 200
        else:
            session['user'] = session.pop('original_admin_user', user)
            return jsonify({'status': 'error', 'message': 'User not found or API error. Admin session restored.'}), 400

    except Exception as e:
        if 'original_admin_user' in session:
            session['user'] = session.pop('original_admin_user')
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/stop-impersonating')
@login_required
def stop_impersonating(user):
    if 'original_admin_user' in session:
        session['user'] = session.pop('original_admin_user')
        session.pop('active_account_id', None)
    return redirect(url_for('dashboard'))

@app.route('/')
@login_required
def dashboard(user):
    print(user)
    combined_str = json.dumps(user)
    encoded_bytes = base64.b64encode(combined_str.encode('utf-8'))
    final_string = encoded_bytes.decode('utf-8')

    return render_template('dashboard.html',user = user)

@app.route('/resources')
@login_required
def resource_hub(user):
    return render_template('resource_hub.html', user = user)

@app.route('/request-access', methods=['POST'])
def request_access():
    user = session.get('user') or {}
    data = request.get_json() or {}
    service_name = data.get('service')
    
    if not service_name:
        return jsonify({'status': 'error', 'message': 'Service name required.'}), 400

    user_email = user.get('email', 'Guest User / Unauthenticated')
    first_name = user.get('first_name', '')
    last_name = user.get('last_name', '')
    user_name = f"{first_name} {last_name}".strip() or 'Guest User'
    account_name = user.get('account') or user.get('reseller_account', 'N/A')

    sales_rep_email = os.environ.get('SALES_REP_EMAIL', 'sales@yourcompany.com')
    from_email = os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@yourcompany.com')
    sendgrid_api_key = os.environ.get('SENDGRID_API_KEY', '').strip()

    if not sendgrid_api_key:
        print("ERROR: SENDGRID_API_KEY environment variable is missing.", flush=True)
        return jsonify({'status': 'error', 'message': 'Email service configuration error.'}), 500

    email_subject = f"ACDI Partner Portal Access Request: {service_name} - {user_email} - {account_name}"
    email_content = f"""
    <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <h2>New Feature Access Request</h2>
            <p><strong>User:</strong> {user_name} ({user_email})</p>
            <p><strong>Account:</strong> {account_name}</p>
            <p><strong>Requested Service:</strong> {service_name}</p>
            <hr>
            <p>Please review and grant access in the admin panel if approved.</p>
        </body>
    </html>
    """

    message = Mail(
        from_email=from_email,
        to_emails=sales_rep_email,
        subject=email_subject,
        html_content=email_content
    )

    if user.get('email'):
        message.reply_to = user_email

    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)

        if response.status_code in [200, 201, 202]:
            print(f"SUCCESS: Access request email sent for {user_email} -> {service_name}", flush=True)
            return jsonify({'status': 'success', 'message': f'Access request submitted for {service_name}.'}), 200
        else:
            print(f"SendGrid API response status: {response.status_code}", flush=True)
            return jsonify({'status': 'error', 'message': 'Failed to deliver request email.'}), 500

    except Exception as e:
        print(f"ERROR: Exception while sending email via SendGrid: {str(e)}", flush=True)
        return jsonify({'status': 'error', 'message': 'Error sending request email.'}), 500

RESELLER_ACCOUNTS_BLOB = 'CACHE/reseller_accounts.json'
api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
api_email = "@acd-inc.com"
ZOHO_RESELLER_API_URL = f"https://www.zohoapis.com/crm/v7/functions/vfreselleraccounts/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"

def refresh_reseller_accounts_gcs():
    """Hits Zoho API and updates reseller_accounts.json in GCS."""
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

        blob.upload_from_string(
            json.dumps(parsed_data, indent=2),
            content_type='application/json'
        )
        print("Successfully refreshed reseller_accounts.json in GCS.", flush=True)
        return parsed_data
    else:
        raise RuntimeError(f"Zoho API error: {data}")

def get_or_update_reseller_accounts():
    """Reads GCS file. If older than 24h or missing, triggers a fresh Zoho API sync."""
    try:
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(RESELLER_ACCOUNTS_BLOB)

        # Check if blob exists and inspect its last updated timestamp
        if blob.exists():
            blob.reload()  # Fetch latest metadata from GCS
            last_updated = blob.updated  # Returns datetime object in UTC
            now = datetime.now(timezone.utc)

            # If cache is younger than 24 hours (86,400 seconds), use it
            if (now - last_updated).total_seconds() < 86400:
                print("Serving reseller accounts from GCS cache...", flush=True)
                return json.loads(blob.download_as_string())

        # If missing or older than 24 hours, update file live
        return refresh_reseller_accounts_gcs()

    except Exception as e:
        print(f"Error accessing GCS cache or Zoho API: {e}", flush=True)
        # Fallback: Attempt reading existing file if API fetch failed
        if 'blob' in locals() and blob.exists():
            return json.loads(blob.download_as_string())
        return {}

@app.route('/rep/accounts')
@login_required
def rep_accounts(user):
    user_email = user.get('email', '')
    permissions = user.get('permissions', [])
    is_admin = 'Admin' in permissions or 'original_admin_user' in session

    try:
        # Force refresh query string support (?refresh=true)
        if request.args.get('refresh') == 'true':
            data = refresh_reseller_accounts_gcs()
        else:
            data = get_or_update_reseller_accounts()

        raw_accounts = data.get("Accounts", [])

        # Filter: Admins see all accounts; Sales Reps see only accounts matching their Owner email
        if is_admin:
            accounts = raw_accounts
        else:
            accounts = [
                acc for acc in raw_accounts 
                if acc.get('Owner.email', '').lower() == user_email.lower()
            ]

        return render_template(
            'rep_accounts.html', 
            user=user, 
            accounts=accounts, 
            total_accounts=len(accounts)
        )

    except Exception as e:
        app.logger.error(f"Error loading rep accounts: {e}")
        return f"Error fetching reseller accounts: {str(e)}", 500

@app.route('/licenses')
@login_required
def reseller_licenses(user):
    user_email = user.get('email') 
    account_id = session.get('active_account_id')
    api_email = user_email

    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_licenses, api_email, account_id)
            
        data = get_cached_licenses(api_email, account_id)
        
        if data.get("code") == "success":
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output)
            
            licenses = parsed_data.get("Licenses", [])
            summary = parsed_data.get("Summary", {})
            
            return render_template('licenses.html', user=user,licenses=licenses,summary=summary)
        else:
            return "API returned an error.", 400
            
    except requests.RequestException as e:
        return f"Error fetching data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing the license data from the API.", 500

@cache.memoize(timeout=14400) 
def get_cached_open_deals(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vf_open_deals/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@app.route('/open-deals')
@login_required
def open_deals(user):
    user_email = user.get('email') 
    api_email = user_email
    

    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_open_deals, api_email)
            
        data = get_cached_open_deals(api_email)
        
        if data.get("code") == "success":
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output)
            
            deals = parsed_data.get("Deals", [])
            total_deals = parsed_data.get("Total_Number_of_Open_Deals", 0)
            
            return render_template('open_deals.html', user=user, deals=deals, total_deals=total_deals)
        else:
            return "API returned an error.", 400
            
    except requests.RequestException as e:
        return f"Error fetching data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing the open deals data from the API.", 500

@app.route('/training')
@login_required
def training(user):
    return render_template('training.html', user=user)

@app.route('/ace')
@login_required
def ace_portal(user):
    if 'ACE' not in user.get('authorized_apps', []):
        return "Unauthorized - You do not have access to the ACE Portal.", 403

    raw_jwt = user.get('token')
    is_impersonating = 'original_admin_user' in session

    return render_template(
        'hubace.html',
        user=user,
        token=raw_jwt,
        is_impersonating=is_impersonating
    )

@app.route('/sales-tools')
@login_required
def sales_tools(user):
    return render_template('sales_tools.html', user=user)



# --- SUPPORT TOOLS ROUTES ---

@app.route('/support-tools')
@login_required
def support_tools(user):
    return render_template('support_tools.html', user=user)

@app.route('/support/create-ticket', methods=['GET', 'POST'])
@login_required
def create_ticket(user):
    if request.method == 'POST':
        data = request.form if request.form else (request.get_json() or {})

        ticket_subject = data.get('ticket_subject', '').strip()
        first_name = data.get('firstname', '').strip() or user.get('first_name', '')
        last_name = data.get('lastname', '').strip() or user.get('last_name', '')
        email = data.get('email', '').strip() or user.get('email', '')
        description = data.get('description', '').strip()
        product_involved = data.get('Product_Involved', '').strip()
        crn = data.get('CRN', '').strip()

        # Input validation
        if not ticket_subject or not description or not product_involved:
            return jsonify({
                'status': 'error', 
                'message': 'Subject, Product Involved, and Description are required.'
            }), 400

        # Account category check (Internal or Reseller)
        account_category = (user.get('account_category') or '').strip().lower()
        reseller_account_name = ""

        if account_category in ['internal', 'reseller']:
            reseller_account_name = user.get('account') or user.get('reseller_account', '')

        # Pass inputs and conditional reseller account name to Zoho function
        ticket_number = vf_create_ticket(
            ticket_subject=ticket_subject,
            firstname=first_name,
            lastname=last_name,
            email=email,
            description=description,
            product_involved=product_involved,
            crn=crn,
            reseller_account_name=reseller_account_name
        )

        if ticket_number != "0":
            return jsonify({
                'status': 'success',
                'message': f'Support ticket #{ticket_number} created successfully!',
                'ticket_number': ticket_number
            }), 200
        else:
            return jsonify({
                'status': 'error', 
                'message': 'Failed to create support ticket in Zoho Desk.'
            }), 500

    default_crn = request.args.get('crn', '')
    return render_template('create_ticket.html', user=user, default_crn=default_crn)

@app.route('/support/open-tickets')
@login_required
def open_tickets(user):
    account_category = (user.get('account_category') or '').strip().lower()

    # Determine account name for reseller users
    account_name = (
        user.get('account') or user.get('reseller_account') or ''
    ).strip()

    tickets = []

    if account_category == 'reseller' and account_name:
        # Handle manual cache refresh via query string (e.g., /support/open-tickets?refresh=true)
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_reseller_open_tickets, account_name)

        tickets = get_reseller_open_tickets(account_name)

    return render_template(
        'open_tickets.html',
        user=user,
        tickets=tickets,
        total_tickets=len(tickets),
        account_name=account_name,
    )



# --- OPEN INSTALLS HELPERS & ROUTE ---

@cache.memoize(timeout=600)  # Caches for 10 minutes
def get_reseller_open_tickets(account_name: str) -> list:
    """Queries Zoho Desk Search API for non-closed tickets matching cf_reseller_account_name."""
    if not account_name:
        return []

    url = "https://desk.zoho.com/api/v1/tickets/search"

    try:
        token = zoho_auth.get_access_token()
    except Exception as ex:
        app.logger.error(f"Failed to fetch Zoho access token: {ex}")
        return []

    headers = {
        "orgId": os.environ.get("ZOHO_ORG_ID", "7868846"),
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json",
    }

    params = {
        "from": 0,
        "limit": 100,
        "departmentId": "20427000000006907",
        "status": "Open,Awaiting Development,Awaiting Customer Response,On Hold,Pending,Resolved,Scheduled",
        "customField1": f"cf_reseller_account_name:{account_name}",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        # /tickets/search returns an array/list of ticket dictionaries
        tickets = data.get("data", []) if isinstance(data, dict) else data
        return tickets if isinstance(tickets, list) else []

    except Exception as ex:
        app.logger.error(f"Error searching reseller tickets in Zoho Desk: {ex}")
        return []

@cache.memoize(timeout=14400)
def get_cached_open_installs(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/getopenpostsalebyrep/actions/execute?auth_type=apikey&zapikey={api_key}&repemail={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@app.route('/support/open-installs')
@login_required
def open_installs(user):
    user_email = user.get('email', '')
    api_email = user_email
    if not user_email.lower().endswith('@acd-inc.com'):
        return "Unauthorized: Open Installs lookup is restricted to internal ACDI employees.", 403
    if user_email.lower().endswith('seth.ingram@acd-inc.com'):
        api_email = 'chris.mcpheeters@acd-inc.com'
    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_open_installs, api_email)

        data = get_cached_open_installs(api_email)

        if data.get("code") == "success":
            details = data.get("details", {})
            
            # 1. Safe parsing of `output`
            raw_output = details.get("output", "[]")
            parsed_installs = []
            
            if isinstance(raw_output, str) and raw_output.strip():
                try:
                    parsed_installs = json.loads(raw_output)
                except json.JSONDecodeError:
                    parsed_installs = []
            elif isinstance(raw_output, list):
                parsed_installs = raw_output

            # Guarantee parsed_installs is a list
            if not isinstance(parsed_installs, list):
                parsed_installs = []

            # 2. Safe parsing of `userMessage` (skips empty strings)
            raw_user_message = details.get("userMessage", [])
            metadata_list = []
            
            if isinstance(raw_user_message, list):
                for item in raw_user_message:
                    if isinstance(item, str) and item.strip():
                        try:
                            metadata_list.append(json.loads(item))
                        except json.JSONDecodeError:
                            pass
                    elif isinstance(item, dict):
                        metadata_list.append(item)

            total_installs = len(parsed_installs)

            return render_template(
                'open_installs.html', 
                user=user, 
                installs=parsed_installs, 
                total_installs=total_installs
            )
        else:
            return "API returned an error loading open installs.", 400

    except requests.RequestException as e:
        return f"Error fetching open installs data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing open installs data from the API.", 500

    # --- PARTNER ADVANTAGE PROGRAM ROUTES ---

@app.route('/partner-advantage')
@login_required
def partner_advantage(user):
    """Main overview landing page for Partner Advantage Program."""
    return render_template('partner_advantage/overview.html', user=user)

@app.route('/partner-advantage/tiers')
@login_required
def partner_advantage_tiers(user):
    """Breakdown of Foundation (Authorized, Select) and Strategic (Premier, Elite) Tiers."""
    return render_template('partner_advantage/tiers.html', user=user)

@app.route('/partner-advantage/drivers')
@login_required
def partner_advantage_drivers(user):
    """Detailed breakdown of the 6 Point Drivers and Scoring Rules."""
    return render_template('partner_advantage/drivers.html', user=user)

@app.route('/partner-advantage/portfolio')
@login_required
def partner_advantage_portfolio(user):
    """Ecosystem Expansion (PaperCut, Scanshare, Intuitive, Hardware, Energy Services, KPAX)."""
    return render_template('partner_advantage/portfolio.html', user=user)

# --- PERKS ROUTES ---

@app.route('/perks/options')
@login_required
def perks_options(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))

    cards_path = os.path.join(app.root_path, 'cards.json')
    brands = []
    catalog_name = "Reward Link Options"

    if os.path.exists(cards_path):
        try:
            with open(cards_path, 'r', encoding='utf-8') as f:
                cards_data = json.load(f)
                catalog_name = cards_data.get('catalogName', catalog_name)
                brands = cards_data.get('brands', [])
        except Exception as e:
            print(f"Error reading cards.json: {e}")

    return render_template(
        'perks/options.html',catalog_name=catalog_name,brands=brands,user=user)
    
@app.route('/perks/home')
@login_required
def perks_home(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    return render_template('perks/home.html',user=user)

@app.route('/perks/rules')
@login_required
def perks_rules(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    return render_template('perks/rules.html',user=user)

@app.route('/perks/terms')
@login_required
def perks_terms(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    return render_template('perks/terms.html',user=user)

@app.route('/perks/redeem', methods=['POST'])
@login_required
def perks_redeem(user):
    user_email = user.get('email')
    data = request.get_json() or {}
    points = data.get('points')

    if not points:
        return jsonify({'status': 'error', 'message': 'Points value is required.'}), 400

    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/perkspointstoreward/actions/execute?auth_type=apikey&zapikey={api_key}&email={user_email}&points={points}"

    try:
        response = requests.get(api_url)
        res_data = response.json()

        cache.delete_memoized(get_cached_rewards, user_email)
        cache.delete_memoized(get_cached_reward_requests, user_email)

        if res_data.get('code') == 'success':
            output_str = res_data.get('details', {}).get('output', '{}')
            parsed_output = json.loads(output_str) if isinstance(output_str, str) else output_str

            if 'Status' in parsed_output and 'Error' in parsed_output['Status']:
                return jsonify({'status': 'error', 'message': parsed_output['Status']}), 400

            return jsonify({'status': 'success', 'data': parsed_output}), 200

        return jsonify({'status': 'error', 'message': res_data.get('message', 'Failed to execute reward request.')}), 400

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/perks/rewards')
@login_required
def perks_rewards(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    user_email = user.get('email') 
    api_email = user_email

    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_rewards, api_email)
            cache.delete_memoized(get_cached_reward_requests, api_email)

        data = get_cached_rewards(api_email)
        
        points = 0
        rewards_user = ""
        
        if data.get("code") == "success" and "details" in data and "output" in data["details"]:
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            
            points = parsed_data.get("Points", 0)
            rewards_user = parsed_data.get("UserName", "")

        req_data = get_cached_reward_requests(api_email)
        reward_requests = []

        if req_data.get("code") == "success" and "details" in req_data and "output" in req_data["details"]:
            raw_req_output = req_data["details"]["output"]
            parsed_req_data = json.loads(raw_req_output) if isinstance(raw_req_output, str) else raw_req_output
            
            if isinstance(parsed_req_data, dict):
                reward_requests = parsed_req_data.get("Claims", parsed_req_data.get("Reward_Requests", parsed_req_data.get("requests", [])))
            elif isinstance(parsed_req_data, list):
                reward_requests = parsed_req_data
            
        return render_template(
            'perks/rewards.html',
            points=points,
            rewards_user=rewards_user,
            reward_requests=reward_requests,
            user=user
        )
    except requests.RequestException as e:
        return f"Error fetching rewards data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing the rewards data from the API.", 500

@app.route('/perks/claim', methods=['GET', 'POST'])
@login_required
def perks_claim(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    today = date.today()
    min_date = today + timedelta(days=-90)
    
    return render_template('perks/claim.html',user=user,max_date=today.strftime("%Y-%m-%d"),min_date=min_date.strftime("%Y-%m-%d"))

@app.route('/perks/claims')
@login_required
def perks_claims(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    user_email = user.get('email')    
    api_email = user_email

    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_claims, api_email)

        data = get_cached_claims(api_email)

        if data.get("code") == "success" and "details" in data and "output" in data["details"]:
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            claims = parsed_data.get("Claims", [])
            summary = parsed_data.get("Summary", {})
        else:
            claims = data.get("Claims", [])
            summary = {}
            
        return render_template('perks/claims.html',claims=claims,summary=summary,user=user)
            
    except requests.RequestException as e:
        return f"Error fetching claims data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing the claims data from the API.", 500


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

@app.route('/employees', methods=['GET'])
@login_required
def manage_employees(user):
    """Renders the management dashboard populated with remote GCS data."""
    employees = load_employee_data()
    return render_template('employees.html', employees=employees)

@app.route('/employees/save_cloud', methods=['POST'])
def save_to_cloud():
    """Formats the updated JSON, backs up the old version, and uploads the new one."""
    try:
        updated_data = request.get_json(force=True)
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for emp in updated_data:
            emp["lastUpdateDate"] = current_time
            first = emp.get('First Name', '').strip()
            last = emp.get('Last Name', '').strip()
            emp["Full Name"] = f"{first} {last}".strip()

        json_output = json.dumps(updated_data, indent=4)

        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(BLOB_NAME)
        
        if blob.exists():
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_blob_name = f"ID/backup/employees_{timestamp_str}.json"
            bucket.copy_blob(blob, bucket, backup_blob_name)
        
        blob.upload_from_string(json_output, content_type='application/json')
        
        blob.cache_control = 'no-cache, max-age=0, must-revalidate'
        blob.patch()

        return jsonify({"status": "success", "message": "Successfully saved and backed up to Google Cloud!"}), 200

    except Exception as e:
        print("--- ERROR IN SAVE TO CLOUD ROUTE ---")
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/employees/download', methods=['POST'])
def download_updated_json():
    """Receives edited JSON data from UI, updates timestamp, and sends file back."""
    try:
        updated_data = request.get_json(force=True) 
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for emp in updated_data:
            emp["lastUpdateDate"] = current_time
            first = emp.get('First Name', '').strip()
            last = emp.get('Last Name', '').strip()
            emp["Full Name"] = f"{first} {last}".strip()

        json_output = json.dumps(updated_data, indent=4)

        buffer = io.BytesIO()
        buffer.write(json_output.encode('utf-8'))
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name='employees.json',
            mimetype='application/json'
        )
    except Exception as e:
        print("--- ERROR IN DOWNLOAD ROUTE ---")
        traceback.print_exc() 
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/employees/list_bucket_images', methods=['GET'])
def list_bucket_images():
    """Lists image files in a specific directory inside the GCS bucket."""
    try:
        prefix = request.args.get('prefix', 'ID/')
        
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        
        blobs = bucket.list_blobs(prefix=prefix)
        
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
        file_list = []
        
        for blob in blobs:
            if blob.name.lower().endswith(valid_extensions):
                public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{blob.name}"
                file_list.append({
                    'path': blob.name,
                    'filename': blob.name.split('/')[-1],
                    'url': public_url
                })
                
        return jsonify({'status': 'success', 'files': file_list}), 200
    
    except Exception as e:
        print("--- ERROR LISTING BUCKET IMAGES ---")
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/upload-image', methods=['POST'])
def upload_image():
    """Receives a local image upload and saves it directly to the GCS bucket."""
    try:
        file = request.files.get('profile_image')
        first_name = request.form.get('first_name')
        last_name = request.form.get('last_name')
        tag_name = request.form.get('tag_name')

        if not file:
            return jsonify({"error": "No file part"}), 400

        extension = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
        folder_name = f"{first_name} {last_name}".strip()
        
        file_name = f"{first_name}_{last_name}_{tag_name}.{extension}"
        blob_path = f"ID/{folder_name}/{file_name}"

        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        
        blob.upload_from_file(file, content_type=file.content_type)
        
        blob.cache_control = 'no-cache, max-age=0, must-revalidate'
        blob.patch()

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_path}"

        return jsonify({"status": "success", "public_path": public_url}), 200

    except Exception as e:
        print("--- ERROR UPLOADING FILE ---")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/directory')
@login_required
def employee_directory(user):
    raw_employees = load_employee_data()
    
    emp_by_email = {
        emp.get('Email Address', '').strip().lower(): emp 
        for emp in raw_employees if emp.get('Email Address')
    }
    
    leadership_team_members = []
    raw_divisions = {}
    
    for emp in raw_employees:
        if emp.get('status') != 'active':
            continue
        
        team_name = emp.get('Team', '').strip() or 'Core Team'
        
        if team_name == 'Leadership Team':
            leadership_team_members.append(emp)
            
        division_name = emp.get('Division', '').strip() or 'General Operations'
        dept_name = emp.get('Department', '').strip() or 'General Department'
        
        if division_name not in raw_divisions:
            raw_divisions[division_name] = {}
        if dept_name not in raw_divisions[division_name]:
            raw_divisions[division_name][dept_name] = {}
        if team_name not in raw_divisions[division_name][dept_name]:
            raw_divisions[division_name][dept_name][team_name] = {}
            
        manager_email = emp.get('Manager Email', '').strip().lower()
        
        if manager_email and manager_email in emp_by_email:
            mgr_obj = emp_by_email[manager_email]
            manager_key = f"{mgr_obj.get('First Name', '')} {mgr_obj.get('Last Name', '')}".strip()
            manager_title = mgr_obj.get('Employee Title', 'Manager')
            manager_name = f"{manager_key} ({manager_title})"
        elif manager_email:
            manager_name = f"Manager ({manager_email})"
        else:
            manager_name = "Executive / Direct Leadership"
            
        if manager_name not in raw_divisions[division_name][dept_name][team_name]:
            raw_divisions[division_name][dept_name][team_name][manager_name] = []
            
        raw_divisions[division_name][dept_name][team_name][manager_name].append(emp)

    divisions = {}
    for div_name, departments in raw_divisions.items():
        sorted_depts = {}
        
        sorted_dept_keys = sorted(
            departments.keys(),
            key=lambda d: (0 if d == 'Leadership Team' else 1, d)
        )
        
        for dept_name in sorted_dept_keys:
            teams = departments[dept_name]
            
            sorted_team_keys = sorted(
                teams.keys(),
                key=lambda t: (0 if 'manager' in t.lower() else 1, t)
            )
            
            sorted_depts[dept_name] = {t_name: teams[t_name] for t_name in sorted_team_keys}
            
        divisions[div_name] = sorted_depts

    return render_template(
        'directory.html', 
        user=user, 
        divisions=divisions,
        leadership_team=leadership_team_members
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)