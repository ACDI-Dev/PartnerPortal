import os
import base64
import json
from functools import wraps
from flask import Flask, render_template, session, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from urllib.parse import urlencode
from datetime import date, timedelta

app = Flask(__name__)

# Tell Flask to trust Google Cloud Run's X-Forwarded-* headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

# A secret key is required to manage Flask sessions locally and in production
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a-very-secure-local-secret')

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
    'c10ff637-e84d-4916-8e90-903a49471355': 'Dummy Application',
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
@app.route('/perks/home')
@login_required
def perks_home(user):
    return render_template('perks/home.html', user=user)


@app.route('/perks/rules')
@login_required
def perks_rules(user):
    return render_template('perks/rules.html', user=user)


@app.route('/perks/terms')
@login_required
def perks_terms(user):
    return render_template('perks/terms.html', user=user)


@app.route('/perks/contact')
@login_required
def perks_contact(user):
    return render_template('perks/contact.html', user=user)

@app.route('/perks/claim', methods=['GET', 'POST'])
@login_required
def perks_claim(user):
        today = date.today()
        min_date = today + timedelta(days=-90)
        return render_template('perks/claim.html', user=user,
                                               max_date=today.strftime("%Y-%m-%d"),
                                               min_date=min_date.strftime("%Y-%m-%d"))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)