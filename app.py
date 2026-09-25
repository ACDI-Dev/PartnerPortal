import os
import base64
import json
import requests
import time
import jwt
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
            print("FusionAuth validation successful (200 OK). Token is valid.", flush=True)
            
            try:
                unverified_claims = jwt.decode(token, options={"verify_signature": False})
                exp_timestamp = unverified_claims.get("exp")
                
                if exp_timestamp:
                    time_to_live = int(exp_timestamp) - int(time.time())
                    cache_timeout = max(0, time_to_live)
                else:
                    cache_timeout = 300 
                    
            except Exception as e:
                print(f"Failed to decode token for expiration calculation: {e}", flush=True)
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

@cache.memoize(timeout=14400) 
def get_cached_licenses(api_email):
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfresellerlicenselookup/actions/execute?auth_type=apikey&zapikey={api_key}&email={api_email}"
    
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

@app.route('/login')
def login():
    redirect_uri = url_for('auth_callback', _external=True)
    return fusionauth.authorize_redirect(redirect_uri)

@app.route('/oauth-callback')
def auth_callback():
    token = fusionauth.authorize_access_token()
    user_info = token.get('userinfo')
    print(user_info)
    id_token = token.get('access_token')
    
    raw_app_ids = user_info.get('authorized_apps', [])
    
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

@app.route('/')
@login_required
def dashboard(user):
    combined_str = json.dumps(user)
    encoded_bytes = base64.b64encode(combined_str.encode('utf-8'))
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
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_licenses, api_email)
            
        data = get_cached_licenses(api_email)
        
        if data.get("code") == "success":
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output)
            
            licenses = parsed_data.get("Licenses", [])
            summary = parsed_data.get("Summary", {})
            
            return render_template(
                'licenses.html', 
                licenses=licenses, 
                summary=summary,
                first_name=session.get('first_name', user.get('first_name', 'Partner')),
                last_name=session.get('last_name', user.get('last_name', '')),
                user=user_email,  
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
    if 'ACE' not in user.get('authorized_apps', []):
        return "Unauthorized - You do not have access to the ACE Portal.", 403

    # Extract the raw JWT instead of base64 encoding the session object
    raw_jwt = user.get('token')

    return render_template('hubace.html',
                           user=user,
                           token=raw_jwt,  
                           email=user.get('email'),
                           first_name=user.get('first_name'),
                           last_name=user.get('last_name'),
                           account=user.get('account'),
                           account_category=user.get('account_category'),
                           reseller_account=user.get('reseller_account'),
                           tier=user.get('tier'),
                           authorized_apps=user.get('authorized_apps'))

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
        'perks/options.html',
        catalog_name=catalog_name,
        brands=brands,
        first_name=session.get('first_name', user.get('first_name', 'Partner')),
        last_name=session.get('last_name', user.get('last_name', '')),
        user=user.get('email'),
        email=user.get('email'),
        reseller_account=session.get('reseller_account', user.get('reseller_account', 'Unknown Account')),
        tier=session.get('tier', user.get('tier', 'Standard')),
        authorized_apps=authorized_apps,
        permissions=session.get('permissions', user.get('permissions', []))
    )
    
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

@app.route('/perks/rewards')
@login_required
def perks_rewards(user):
    authorized_apps = user.get('authorized_apps', [])
    if 'Partner Perks' not in authorized_apps:
        return redirect(url_for('dashboard'))
        
    user_email = user.get('email') 
    
    api_email = user_email
    if user_email and '@acd-inc.com' in user_email.lower():
        api_email = 'eknight@tomorrowsoffice.com'
    
    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_rewards, api_email)

        data = get_cached_rewards(api_email)
        
        points = 0
        rewards_user = ""
        
        if data.get("code") == "success" and "details" in data and "output" in data["details"]:
            raw_output = data["details"]["output"]
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            
            points = parsed_data.get("Points", 0)
            rewards_user = parsed_data.get("UserName", "")
            
        return render_template(
            'perks/rewards.html', 
            points=points,
            rewards_user=rewards_user,
            first_name=session.get('first_name', user.get('first_name', 'Partner')),
            last_name=session.get('last_name', user.get('last_name', '')),
            user=user_email,
            email=user_email,
            reseller_account=session.get('reseller_account', user.get('reseller_account', 'Unknown Account')),
            tier=session.get('tier', user.get('tier', 'Standard')),
            authorized_apps=authorized_apps,
            permissions=session.get('permissions', user.get('permissions', []))
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