import os
from urllib.parse import urlencode
from flask import Blueprint, session, redirect, url_for, request, jsonify
from extensions import oauth
from auth.fusionauth import AUTHORIZED_APP_MAP, login_required

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login')
def login():
    redirect_uri = url_for('auth.auth_callback', _external=True)
    return oauth.fusionauth.authorize_redirect(redirect_uri)

@auth_bp.route('/oauth-callback')
def auth_callback():
    token = oauth.fusionauth.authorize_access_token()
    user_info = token.get('userinfo')
    access_token = token.get('access_token')
    refresh_token = token.get('refresh_token')
    
    raw_app_ids = user_info.get('authorized_apps', [])
    REQUIRED_APP_ID = '10ec4e31-10a5-417a-92e3-24b886e1c750'
    
    if REQUIRED_APP_ID not in raw_app_ids:
        return "Unauthorized: You are not registered for the ACDI Reseller Portal.", 403
    
    friendly_apps = [AUTHORIZED_APP_MAP.get(app_id, f'Unknown Application ({app_id})') for app_id in raw_app_ids]
    
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
        'token': access_token,
        'refresh_token': refresh_token
    }
    
    next_url = session.pop('next', url_for('main.dashboard'))
    return redirect(next_url)

@auth_bp.route('/logout')
def logout():
    session.clear()
    client_id = os.environ.get('FUSIONAUTH_CLIENT_ID')
    fusionauth_url = os.environ.get('FUSIONAUTH_URL')
    post_logout_redirect_uri = url_for('auth.login', _external=True) 
    
    params = {'client_id': client_id, 'post_logout_redirect_uri': post_logout_redirect_uri}
    logout_url = f"{fusionauth_url}/oauth2/logout?{urlencode(params)}"
    return redirect(logout_url)

@auth_bp.route('/impersonate', methods=['POST'])
@login_required
def impersonate(user):
    is_admin = 'Admin' in user.get('permissions', []) or 'original_admin_user' in session
    if not is_admin:
        return jsonify({'status': 'error', 'message': 'Unauthorized.'}), 403

    data = request.get_json()
    target_email = data.get('email')
    if not target_email:
        return jsonify({'status': 'error', 'message': 'Email address required.'}), 400

    import requests, json
    api_key = "1003.1b041a7343e84025c8361f86ba9bd6c2.77be6b286da0fa40d8defcd4bdc4fd29"
    api_url = f"https://www.zohoapis.com/crm/v7/functions/vfloginas/actions/execute?auth_type=apikey&zapikey={api_key}&emailString={target_email}"

    try:
        response = requests.get(api_url)
        res_data = response.json()

        if 'original_admin_user' not in session:
            session['original_admin_user'] = user

        if res_data.get("code") == "success":
            raw_output = res_data.get("details", {}).get("output", "{}")
            parsed_user = json.loads(raw_output) if isinstance(raw_output, str) else raw_output

            target_account_id = parsed_user.get('account_id', '')

            session['user'] = {
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
                'token': user.get('token'),
                'refresh_token': user.get('refresh_token')
            }
            
            if target_account_id:
                session['active_account_id'] = target_account_id

            return jsonify({'status': 'success', 'message': f'Impersonating {target_email}'}), 200
        else:
            session['user'] = session.pop('original_admin_user', user)
            return jsonify({'status': 'error', 'message': 'User not found.'}), 400
    except Exception as e:
        if 'original_admin_user' in session:
            session['user'] = session.pop('original_admin_user')
        return jsonify({'status': 'error', 'message': str(e)}), 500

@auth_bp.route('/stop-impersonating')
@login_required
def stop_impersonating(user):
    if 'original_admin_user' in session:
        session['user'] = session.pop('original_admin_user')
        session.pop('active_account_id', None)
    return redirect(url_for('main.dashboard'))