import os
from functools import wraps
from flask import Flask, request, render_template

from google.auth.transport import requests
from google.oauth2 import id_token

app = Flask(__name__)

# The magic switch for local development
MOCK_IAP = os.environ.get('MOCK_IAP', 'True').lower() == 'true'

# Format: /projects/<PROJECT_NUMBER>/global/backendServices/<SERVICE_ID>
IAP_AUDIENCE = os.environ.get('IAP_AUDIENCE', '')

def get_user_identity():
    """Extracts the user identity from IAP headers, or uses mock data locally."""
    if MOCK_IAP:
        # LOCAL TESTING BYPASS
        return {
            'email': 'partner@localdev.com',
            'first_name': 'Jane',
            'last_name': 'Doe',
            'reseller_account': 'ACME Corp Solutions',
            'tier': 'Platinum',
            'permissions': ['Quote_Create', 'MDF_Request', 'Support_Admin', 'ACE_Access'] 
        }
        
    # PRODUCTION IAP FLOW
    iap_jwt = request.headers.get('x-goog-iap-jwt-assertion')

    if not iap_jwt:
        return None

    try:
        # Cryptographically verify the token was signed by Google and meant for this app
        decoded_jwt = id_token.verify_token(
            iap_jwt, 
            requests.Request(), 
            audience=IAP_AUDIENCE,
            certs_url='https://www.gstatic.com/iap/verify/public_key'
        )
        
        user = {'email': decoded_jwt.get('email')}
        
        # Extract custom SAML assertions mapped by Identity Platform
        firebase_claims = decoded_jwt.get('firebase', {}).get('identities', {})
        
        # New assertions
        user['first_name'] = firebase_claims.get('firstName', [''])[0]
        user['last_name'] = firebase_claims.get('lastName', [''])[0]
        user['reseller_account'] = firebase_claims.get('resellerAccount', [''])[0]
        
        # Original assertions
        user['tier'] = firebase_claims.get('partnerTier', ['Standard'])[0]
        user['permissions'] = firebase_claims.get('permissions', [])
        
        return user

    except Exception as e:
        print(f"JWT Validation failed: {e}")
        return None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user_identity()
        if not user:
            return "Unauthorized - Missing or Invalid IAP Identity", 401
        return f(user=user, *args, **kwargs)
    return decorated_function

@app.route('/')
@login_required
def dashboard(user):
    return render_template('dashboard.html', 
                           email=user.get('email'),
                           first_name=user.get('first_name'),
                           last_name=user.get('last_name'),
                           reseller_account=user.get('reseller_account'),
                           tier=user.get('tier'),
                           permissions=user.get('permissions'))

@app.route('/training')
@login_required
def training(user):
    return render_template('training.html', 
                           email=user.get('email'),
                           first_name=user.get('first_name'),
                           last_name=user.get('last_name'),
                           reseller_account=user.get('reseller_account'),
                           tier=user.get('tier'),
                           permissions=user.get('permissions'))

@app.route('/ace')
@login_required
def ace_portal(user):
    # Check if the user actually has the ACE_Access assertion
    if 'ACE_Access' not in user.get('permissions', []):
        return "Unauthorized - You do not have access to the ACE Portal.", 403

    return render_template('hubace.html', 
                           email=user.get('email'),
                           first_name=user.get('first_name'),
                           last_name=user.get('last_name'),
                           reseller_account=user.get('reseller_account'),
                           tier=user.get('tier'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)