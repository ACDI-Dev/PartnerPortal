import os
import time
import jwt
import requests
from functools import wraps
from flask import session, redirect, url_for, request
from extensions import cache

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

def refresh_user_token(refresh_token):
    fusionauth_url = os.environ.get('FUSIONAUTH_URL')
    client_id = os.environ.get('FUSIONAUTH_CLIENT_ID')
    client_secret = os.environ.get('FUSIONAUTH_CLIENT_SECRET')

    token_url = f"{fusionauth_url}/oauth2/token"
    payload = {
        'grant_type': 'refresh_token',
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token
    }

    try:
        response = requests.post(token_url, data=payload, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data.get('access_token'), data.get('refresh_token', refresh_token)
    except Exception as e:
        print(f"Error refreshing token: {e}", flush=True)

    return None, None

def is_token_valid(token):
    if not token:
        return False
        
    cache_key = f"token_valid:{token}"
    cached_result = cache.get(cache_key)
    
    if cached_result is not None:
        return cached_result
        
    fusionauth_url = os.environ.get('FUSIONAUTH_URL')
    if not fusionauth_url:
        return False
        
    validate_url = f"{fusionauth_url}/api/jwt/validate"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        response = requests.get(validate_url, headers=headers)
        
        if response.status_code == 200:
            try:
                unverified_claims = jwt.decode(token, options={"verify_signature": False})
                REQUIRED_APP_ID = '10ec4e31-10a5-417a-92e3-24b886e1c750'
                
                if unverified_claims.get("applicationId") != REQUIRED_APP_ID:
                    cache.set(cache_key, False, timeout=60)
                    return False
                
                exp_timestamp = unverified_claims.get("exp")
                cache_timeout = max(0, int(exp_timestamp) - int(time.time())) if exp_timestamp else 300
                    
            except Exception:
                cache_timeout = 300 
                
            cache.set(cache_key, True, timeout=cache_timeout)
            return True
        else:
            user = session.get('user')
            if user and user.get('refresh_token'):
                new_access_token, new_refresh_token = refresh_user_token(user['refresh_token'])
                if new_access_token:
                    session['user']['token'] = new_access_token
                    session['user']['refresh_token'] = new_refresh_token
                    session.modified = True
                    return is_token_valid(new_access_token)

            cache.set(cache_key, False, timeout=60)
            return False
            
    except requests.RequestException:
        return False

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user_identity()
        if not user:
            session['next'] = request.url
            return redirect(url_for('auth.login'))
            
        token = user.get('token')
        if not is_token_valid(token):
            next_url = request.url
            session.clear()  
            session['next'] = next_url
            return redirect(url_for('auth.login'))
            
        return f(user=user, *args, **kwargs)
    return decorated_function