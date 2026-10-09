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
                    # Cache the failure to prevent repeatedly validating unauthorized tokens
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
        
        # 1. Check if the user exists in the local session
        if not user:
            # Store the requested URL before redirecting
            session['next'] = request.url
            return redirect(url_for('login'))
            
        # 2. Extract the stored access token
        token = user.get('token')
        
        # 3. Validate the token with FusionAuth 
        if not is_token_valid(token):
            # Store the requested URL BEFORE clearing the session
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
    #print("Reward requests API response:", response.text, flush=True)
    return response.json()

@cache.memoize(timeout=14400) 
def get_cached_rep_accounts(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfreselleraccounts/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

# Modify your existing get_cached_licenses to accept an optional account_id
@cache.memoize(timeout=14400) 
def get_cached_licenses(api_email, account_id=None):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    
    # Base URL
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfresellerlicenselookup/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    # Append account_id to the Zoho function if provided by the rep's selection
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


# --- AUTH ROUTES ---

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
    # 1. Pull active account_id from session or fallback to user's direct account_id
    account_id = session.get('active_account_id') or user.get('account_id', '')
    user_email = user.get('email', '')
    
    # 2. Default fallback for internal ACDI employees if no account is tied
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
    # Store the account_id securely in the user's session
    session['active_account_id'] = account_id
    
    # Redirect to the licenses page (the URL will just be /licenses)
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
    
    # --- NEW: Deny access if they don't have the required app ---
    REQUIRED_APP_ID = '10ec4e31-10a5-417a-92e3-24b886e1c750'
    if REQUIRED_APP_ID not in raw_app_ids:
        # Clear the FusionAuth SSO session on denial (optional but recommended)
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
    
    # Extract the next URL and clear it from the session, default to dashboard
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
    # Ensure only Admins can execute this
    if 'Admin' not in user.get('permissions', []):
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

        # Save original admin session if not already set
        if 'original_admin_user' not in session:
            session['original_admin_user'] = user

        if res_data.get("code") == "success":
            raw_output = res_data.get("details", {}).get("output", "{}")
            user_messages = res_data.get("details", {}).get("userMessage", [])
            parsed_user = json.loads(raw_output) if isinstance(raw_output, str) else raw_output

            # Check if record was not found
            if not parsed_user or "No record found with that email address." in user_messages:
                session['user'] = session.pop('original_admin_user', user)
                warning_msg = user_messages[0] if user_messages else f"User record '{target_email}' could not be found."
                return jsonify({
                    'status': 'warning',
                    'message': f"{warning_msg} Admin session restored."
                }), 404

            # Extract account_id from the parsed user payload
            target_account_id = parsed_user.get('account_id', '')

            impersonated_user = {
                'email': parsed_user.get('email'),
                'first_name': parsed_user.get('first_name', ''),
                'last_name': parsed_user.get('last_name', ''),
                'account': parsed_user.get('account', ''),
                'account_category': parsed_user.get('account_category', ''),
                'reseller_account': parsed_user.get('reseller_account', ''),
                'account_id': target_account_id,  # Saved in user dict
                'tier': parsed_user.get('tier', 'Standard'),
                'permissions': parsed_user.get('permissions', []),
                'authorized_apps': parsed_user.get('authorized_apps', []),
                'token': user.get('token') 
            }

            session['user'] = impersonated_user
            
            # Automatically set active_account_id to the impersonated user's account ID
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
    # Restore the original admin user session
    if 'original_admin_user' in session:
        session['user'] = session.pop('original_admin_user')
        session.pop('active_account_id', None) # Clear it on the way back out, too
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

    # Fall back to user session details if present, otherwise default to anonymous/guest details
    user_email = user.get('email', 'Guest User / Unauthenticated')
    first_name = user.get('first_name', '')
    last_name = user.get('last_name', '')
    user_name = f"{first_name} {last_name}".strip() or 'Guest User'
    account_name = user.get('account') or user.get('reseller_account', 'N/A')

    # Target email address for the Sales Rep
    sales_rep_email = os.environ.get('SALES_REP_EMAIL', 'sales@yourcompany.com')
    from_email = os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@yourcompany.com')
    sendgrid_api_key = os.environ.get('SENDGRID_API_KEY', '').strip()

    if not sendgrid_api_key:
        print("ERROR: SENDGRID_API_KEY environment variable is missing.", flush=True)
        return jsonify({'status': 'error', 'message': 'Email service configuration error.'}), 500

    # Build the Email Message
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

@app.route('/rep/accounts')
@login_required
def rep_accounts(user):
    user_email = user.get('email') 
    permissions=user.get('permissions')
   # Use the rep's email for the API call (or override for testing as you did previously)
    api_email = user_email
    if 'Admin' in permissions:
        api_email = '@acd-inc.com'
    
    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_rep_accounts, api_email)
            
        data = get_cached_rep_accounts(api_email)
        
        if data.get("code") == "success":
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output)
            
            accounts = parsed_data.get("Accounts", [])
            total_accounts = parsed_data.get("Total_Number_of_Accounts", 0)
            
            return render_template('rep_accounts.html', user=user,accounts=accounts,total_accounts=total_accounts)
        else:
            return "API returned an error.", 400
            
    except requests.RequestException as e:
        return f"Error fetching data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing the accounts data from the API.", 500

# Modify your existing licenses route to read the account_id from the URL query params
@app.route('/licenses')
@login_required
def reseller_licenses(user):
    user_email = user.get('email') 
    
    # Pull the account_id from the session instead of the URL
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
    
    if user_email and '@acd-inc.com' in user_email.lower():
        api_email = 'eknight@tomorrowsoffice.com'
          
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

    # Extract the raw JWT
    raw_jwt = user.get('token')
    
    # Check if currently in impersonate mode
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

        # Invalidate caches so updated balance/requests reload
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

        # Fetch reward points
        data = get_cached_rewards(api_email)
        
        points = 0
        rewards_user = ""
        
        if data.get("code") == "success" and "details" in data and "output" in data["details"]:
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            
            points = parsed_data.get("Points", 0)
            rewards_user = parsed_data.get("UserName", "")

        # Fetch reward requests
        req_data = get_cached_reward_requests(api_email)
        reward_requests = []

        if req_data.get("code") == "success" and "details" in req_data and "output" in req_data["details"]:
            raw_req_output = req_data["details"]["output"]
            parsed_req_data = json.loads(raw_req_output) if isinstance(raw_req_output, str) else raw_req_output
            
            # Extract reward requests list from "Claims" key
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
    if user_email and '@acd-inc.com' in user_email.lower():
        api_email = 'eknight@tomorrowsoffice.com'
    
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
        
        # Download the file contents as a string
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

        # Automatically update lastUpdateDate and synchronize Full Name
        for emp in updated_data:
            emp["lastUpdateDate"] = current_time
            first = emp.get('First Name', '').strip()
            last = emp.get('Last Name', '').strip()
            emp["Full Name"] = f"{first} {last}".strip()

        # Format JSON with indenting for clean human-readable output
        json_output = json.dumps(updated_data, indent=4)

        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(BLOB_NAME)
        
        # --- NEW: BACKUP EXISTING FILE ---
        # Check if the master file already exists, and if so, copy it to the backup folder
        if blob.exists():
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_blob_name = f"ID/backup/employees_{timestamp_str}.json"
            bucket.copy_blob(blob, bucket, backup_blob_name)
        # ---------------------------------
        
        # Upload the new data to GCS
        blob.upload_from_string(json_output, content_type='application/json')
        
        # Ensure cache control is set so edge servers fetch the fresh file
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
        # force=True ensures it parses the JSON even if headers get stripped
        updated_data = request.get_json(force=True) 
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Automatically update lastUpdateDate and synchronize Full Name
        for emp in updated_data:
            emp["lastUpdateDate"] = current_time
            first = emp.get('First Name', '').strip()
            last = emp.get('Last Name', '').strip()
            emp["Full Name"] = f"{first} {last}".strip()

        # Format JSON with indenting for clean human-readable output
        json_output = json.dumps(updated_data, indent=4)

        # Create an in-memory file stream for browser download
        buffer = io.BytesIO()
        buffer.write(json_output.encode('utf-8'))
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name='employees.json', # CHANGE TO attachment_filename='employees.json' IF ON FLASK 1.x
            mimetype='application/json'
        )
    except Exception as e:
        # This will print the exact line and error to your Python terminal
        print("--- ERROR IN DOWNLOAD ROUTE ---")
        traceback.print_exc() 
        return jsonify({"status": "error", "message": str(e)}), 400
@app.route('/employees/list_bucket_images', methods=['GET'])
def list_bucket_images():
    """Lists image files in a specific directory inside the GCS bucket."""
    try:
        # You can specify a prefix/subfolder here (e.g., 'ID/' or 'ID/images/')
        prefix = request.args.get('prefix', 'ID/')
        
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        
        # Fetch blobs starting with the prefix
        blobs = bucket.list_blobs(prefix=prefix)
        
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
        file_list = []
        
        for blob in blobs:
            # Filter out folder placeholders and non-image files
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
        # Grab the file and form data sent by your new JS function
        file = request.files.get('profile_image')
        first_name = request.form.get('first_name')
        last_name = request.form.get('last_name')
        tag_name = request.form.get('tag_name')

        if not file:
            return jsonify({"error": "No file part"}), 400

        # Extract the file extension
        extension = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
        
        # Define the folder and the new specific file name format
        folder_name = f"{first_name} {last_name}".strip()
        
        # NEW FORMAT: First Name_Last Name_tag.extension
        file_name = f"{first_name}_{last_name}_{tag_name}.{extension}"
        blob_path = f"ID/{folder_name}/{file_name}"
        # Upload to Google Cloud Storage
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        
        # Read the file from memory and upload it
        blob.upload_from_file(file, content_type=file.content_type)
        
        # Ensure cache control is set so new images show up immediately
        blob.cache_control = 'no-cache, max-age=0, must-revalidate'
        blob.patch()

        # Construct the public URL to return to the frontend
        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_path}"

        return jsonify({"status": "success", "public_path": public_url}), 200

    except Exception as e:
        print("--- ERROR UPLOADING FILE ---")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/directory')
@login_required
def employee_directory(user):
    raw_employees = load_employee_data()
    
    # Map email to employee dict for quick manager lookup
    emp_by_email = {
        emp.get('Email Address', '').strip().lower(): emp 
        for emp in raw_employees if emp.get('Email Address')
    }
    
    # Track top-level leadership team members separately
    leadership_team_members = []
    
    # Nesting hierarchy: Division -> Department -> Team -> Manager -> Employees
    raw_divisions = {}
    
    for emp in raw_employees:
        # Filter active employees only
        if emp.get('status') != 'active':
            continue
        
        team_name = emp.get('Team', '').strip() or 'Core Team'
        
        # Collect top-level Leadership Team members
        if team_name == 'Leadership Team':
            leadership_team_members.append(emp)
            
        division_name = emp.get('Division', '').strip() or 'General Operations'
        dept_name = emp.get('Department', '').strip() or 'General Department'
        
        # Initialize nested structures
        if division_name not in raw_divisions:
            raw_divisions[division_name] = {}
        if dept_name not in raw_divisions[division_name]:
            raw_divisions[division_name][dept_name] = {}
        if team_name not in raw_divisions[division_name][dept_name]:
            raw_divisions[division_name][dept_name][team_name] = {}
            
        manager_email = emp.get('Manager Email', '').strip().lower()
        
        # Determine manager name & title
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

    # Sort hierarchy:
    # 1. Departments: "Leadership Team" first, then alphabetical
    # 2. Teams: Teams containing "Manager" first, then alphabetical
    divisions = {}
    for div_name, departments in raw_divisions.items():
        sorted_depts = {}
        
        # Sort department keys
        sorted_dept_keys = sorted(
            departments.keys(),
            key=lambda d: (0 if d == 'Leadership Team' else 1, d)
        )
        
        for dept_name in sorted_dept_keys:
            teams = departments[dept_name]
            
            # Sort team keys: Teams with "Manager" in their name come first
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