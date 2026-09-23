import os
import base64
import json
import requests
from functools import wraps
from flask import Flask, render_template, session, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from urllib.parse import urlencode
from datetime import date, timedelta
from flask_caching import Cache

app = Flask(__name__)

# Tell Flask to trust Google Cloud Run's X-Forwarded-* headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

# A secret key is required to manage Flask sessions locally and in production
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a-very-secure-local-secret')

# --- CACHING SETUP ---
# Use an environment variable for the Redis URL (e.g., redis://10.0.0.5:6379)
redis_url = os.environ.get('REDIS_URL')

if redis_url:
    # Production: Unified Redis Cache across all Google Cloud Run instances
    app.config['CACHE_TYPE'] = 'RedisCache'
    app.config['CACHE_REDIS_URL'] = redis_url
else:
    # Local Development: Fall back to simple RAM cache if no Redis URL is found
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

def get_user_identity():
    """Extracts the user identity from the Flask session."""
    return session.get('user')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user_identity()
        if not user:
            # Redirect to the login route if no session exists
            return redirect(url_for('login'))
        return f(user=user, *args, **kwargs)
    return decorated_function


# --- CACHED API HELPERS ---

@cache.memoize(timeout=14400) # Caches results per email for 4 hours
def get_cached_licenses(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfresellerlicenselookup/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

@cache.memoize(timeout=14400) # Caches results per email for 4 hours
def get_cached_claims(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/perks_claims_list/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()


# --- AUTH ROUTES ---

@app.route('/login')
def login():
    """Redirects the user to the FusionAuth login page."""
    redirect_uri = url_for('auth_callback', _external=True)
    return fusionauth.authorize_redirect(redirect_uri)

@app.route('/oauth-callback')
def auth_callback():
    token = fusionauth.authorize_access_token()
    user_info = token.get('userinfo')
    print(user_info)
    id_token = token.get('access_token')
    # 1. Get the raw list of UUIDs from the JWT
    raw_app_ids = user_info.get('authorized_apps', [])
    
    # 2. Map the UUIDs to human-friendly names using our dictionary
    friendly_apps = []
    for app_id in raw_app_ids:
        # If the ID exists in our map, grab the friendly name. 
        # If it's a new ID we haven't mapped yet, just use 'Unknown Application'
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
        # 3. Store the clean, human-readable list in the session
        'authorized_apps': friendly_apps,
        'token': id_token
    }
    
    return redirect(url_for('dashboard'))


@app.route('/logout')
def logout():
    """Clears the local session and redirects to FusionAuth to kill the SSO session."""
    # 1. Clear the local Flask session
    session.clear()
    
    # 2. Build the FusionAuth logout URL
    client_id = os.environ.get('FUSIONAUTH_CLIENT_ID')
    fusionauth_url = os.environ.get('FUSIONAUTH_URL')
    
    # Where FusionAuth should send the user AFTER they are logged out
    post_logout_redirect_uri = url_for('login', _external=True) 
    
    # 3. Redirect the user to FusionAuth
    params = {
        'client_id': client_id,
        'post_logout_redirect_uri': post_logout_redirect_uri
    }
    logout_url = f"{fusionauth_url}/oauth2/logout?{urlencode(params)}"
    
    return redirect(logout_url)


# --- MAIN APPLICATION ROUTES ---

@app.route('/')
@login_required
def dashboard(user):
    combined_str = json.dumps(user)
    # 2. Convert string to UTF-8 bytes, then encode to Base64
    encoded_bytes = base64.b64encode(combined_str.encode('utf-8'))

    # 3. Convert bytes back to a clean string for transport
    final_string = encoded_bytes.decode('utf-8')

    return render_template('dashboard.html', 
                           user = final_string,
                           email=user.get('email'),
                           first_name=user.get('first_name'),
                           last_name=user.get('last_name'),
                           account=user.get('account'),
                           reseller_account=user.get('reseller_account'),
                           tier=user.get('tier'),
                           permissions=user.get('permissions'),
                           authorized_apps=user.get('authorized_apps'),                   
                           token = user.get('token'))

@app.route('/licenses')
@login_required
def reseller_licenses(user):
    user_email = user.get('email') 
    
    api_email = user_email
    if user_email and '@acd-inc.com' in user_email.lower():
        api_email = 'eknight@tomorrowsoffice.com'
    
    try:
        # Check if the user clicked "Force Refresh"
        if request.args.get('refresh') == 'true':
            # This deletes the specific cached entry for this email
            cache.delete_memoized(get_cached_licenses, api_email)
            
        # Fetch from cache (or API if we just cleared the cache)
        data = get_cached_licenses(api_email)
        
        if data.get("code") == "success":
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output)
            
            licenses = parsed_data.get("Licenses", [])
            summary = parsed_data.get("Summary", {})
            
            # Render template and pass along the layout.html requirements
            return render_template(
                'licenses.html', 
                licenses=licenses, 
                summary=summary,
                first_name=session.get('first_name', user.get('first_name', 'Partner')),
                last_name=session.get('last_name', user.get('last_name', '')),
                user=user_email,  # Keeps their real email in the UI
                reseller_account=session.get('reseller_account', user.get('reseller_account', 'Unknown Account')),
                tier=session.get('tier', user.get('tier', 'Standard')),
                authorized_apps=session.get('authorized_apps', user.get('authorized_apps', [])),
                permissions=session.get('permissions', user.get('permissions', []))
            )
        else:
            return "API returned an error.", 400
            
    except requests.RequestException as e:
        return f"Error fetching data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing the license data from the API.", 500

@app.route('/training')
@login_required
def training(user):
    return render_template('training.html', 
                           email=user.get('email'),
                           first_name=user.get('first_name'),
                           last_name=user.get('last_name'),
                           reseller_account=user.get('reseller_account'),
                           tier=user.get('tier'),
                           permissions=user.get('permissions'),
                           authorized_apps=user.get('authorized_apps'))

@app.route('/ace')
@login_required
def ace_portal(user):
    # Check if the user actually has the ACE_Access assertion
    if 'ACE' not in user.get('authorized_apps', []):
        return "Unauthorized - You do not have access to the ACE Portal.", 403
    #1 json convert to string
    combined_str = json.dumps(user)
    # 2. Convert string to UTF-8 bytes, then encode to Base64
    encoded_bytes = base64.b64encode(combined_str.encode('utf-8'))

    # 3. Convert bytes back to a clean string for transport
    final_string = encoded_bytes.decode('utf-8')

    return render_template('hubace.html',
                           user = user,
                           token = final_string,
                           email=user.get('email'),
                           first_name=user.get('first_name'),
                           last_name=user.get('last_name'),
                           account=user.get('account'),
                           account_category=user.get('account_category'),
                           reseller_account=user.get('reseller_account'),
                           tier=user.get('tier'),
                           authorized_apps=user.get('authorized_apps'))


# --- PERKS ROUTES ---

@app.route('/perks/home')
@login_required
def perks_home(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    return render_template('perks/home.html',
            email=user.get('email'),
            first_name=user.get('first_name'),
            last_name=user.get('last_name'),
            account=user.get('account'),
            account_category=user.get('account_category'),
            reseller_account=user.get('reseller_account'),
            tier=user.get('tier'),
            authorized_apps=user.get('authorized_apps'),
            permissions=session.get('permissions', user.get('permissions', [])))


@app.route('/perks/rules')
@login_required
def perks_rules(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    return render_template('perks/rules.html',
            email=user.get('email'),
            first_name=user.get('first_name'),
            last_name=user.get('last_name'),
            account=user.get('account'),
            account_category=user.get('account_category'),
            reseller_account=user.get('reseller_account'),
            tier=user.get('tier'),
            authorized_apps=user.get('authorized_apps'),
            permissions=session.get('permissions', user.get('permissions', [])))


@app.route('/perks/terms')
@login_required
def perks_terms(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    return render_template('perks/terms.html',
            email=user.get('email'),
            first_name=user.get('first_name'),
            last_name=user.get('last_name'),
            account=user.get('account'),
            account_category=user.get('account_category'),
            reseller_account=user.get('reseller_account'),
            tier=user.get('tier'),
            authorized_apps=user.get('authorized_apps'),
            permissions=session.get('permissions', user.get('permissions', [])))

@app.route('/perks/contact')
@login_required
def perks_contact(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    return render_template('perks/contact.html',
            email=user.get('email'),
            first_name=user.get('first_name'),
            last_name=user.get('last_name'),
            account=user.get('account'),
            account_category=user.get('account_category'),
            reseller_account=user.get('reseller_account'),
            tier=user.get('tier'),
            authorized_apps=user.get('authorized_apps'),
            permissions=session.get('permissions', user.get('permissions', [])))


@app.route('/perks/claim', methods=['GET', 'POST'])
@login_required
def perks_claim(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    today = date.today()
    min_date = today + timedelta(days=-90)
    
    return render_template('perks/claim.html',
            email=user.get('email'),
            first_name=user.get('first_name'),
            last_name=user.get('last_name'),
            account=user.get('account'),
            account_category=user.get('account_category'),
            reseller_account=user.get('reseller_account'),
            tier=user.get('tier'),
            user=user,
            authorized_apps=user.get('authorized_apps'),
            permissions=session.get('permissions', user.get('permissions', [])),
            max_date=today.strftime("%Y-%m-%d"),
            min_date=min_date.strftime("%Y-%m-%d"))

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
        # Check if the user clicked "Force Refresh"
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_claims, api_email)

        # Fetch from cache (or API if we just cleared the cache)
        data = get_cached_claims(api_email)

        # Safely parse stringified details from Zoho
        if data.get("code") == "success" and "details" in data and "output" in data["details"]:
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            claims = parsed_data.get("Claims", [])
            summary = parsed_data.get("Summary", {})
        else:
            claims = data.get("Claims", [])
            summary = {}
            
        return render_template(
            'perks/claims.html', 
            claims=claims, 
            summary=summary,
            first_name=session.get('first_name', user.get('first_name', 'Partner')),
            last_name=session.get('last_name', user.get('last_name', '')),
            user=user,
            email=user_email,
            reseller_account=session.get('reseller_account', user.get('reseller_account', 'Unknown Account')),
            tier=session.get('tier', user.get('tier', 'Standard')),
            authorized_apps=authorized_apps,
            permissions=session.get('permissions', user.get('permissions', []))
        )
            
    except requests.RequestException as e:
        return f"Error fetching claims data: {str(e)}", 500
    except json.JSONDecodeError:
        return "Error parsing the claims data from the API.", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)