import os
import json
import base64
from flask import Blueprint, render_template, request, jsonify, session
from auth.fusionauth import login_required
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
@login_required
def dashboard(user):
    combined_str = json.dumps(user)
    encoded_bytes = base64.b64encode(combined_str.encode('utf-8'))
    return render_template('dashboard.html', user=user)

@main_bp.route('/resources')
@login_required
def resource_hub(user):
    return render_template('resource_hub.html', user=user)

@main_bp.route('/training')
@login_required
def training(user):
    return render_template('training.html', user=user)

@main_bp.route('/tools/cardscriptcreator')
def cardscriptcreator(user):
    return render_template('cardscriptcreator.html', user=user)

@main_bp.route('/ace')
@login_required
def ace_portal(user):
    if 'ACE' not in user.get('authorized_apps', []):
        return "Unauthorized - You do not have access to the ACE Portal.", 403

    return render_template(
        'hubace.html',
        user=user,
        token=user.get('token'),
        is_impersonating='original_admin_user' in session
    )

@main_bp.route('/request-access', methods=['POST'])
def request_access():
    user = session.get('user') or {}
    data = request.get_json() or {}
    service_name = data.get('service')
    
    if not service_name:
        return jsonify({'status': 'error', 'message': 'Service name required.'}), 400

    user_email = user.get('email', 'Guest User / Unauthenticated')
    user_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or 'Guest User'
    account_name = user.get('account')

    sales_rep_email = os.environ.get('SALES_REP_EMAIL', 'sales@yourcompany.com')
    from_email = os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@yourcompany.com')
    sendgrid_api_key = os.environ.get('SENDGRID_API_KEY', '').strip()

    if not sendgrid_api_key:
        return jsonify({'status': 'error', 'message': 'Email service configuration error.'}), 500

    email_subject = f"ACDI Partner Portal Access Request: {service_name} - {user_email} - {account_name}"
    email_content = f"""
    <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <h2>New Feature Access Request</h2>
            <p><strong>User:</strong> {user_name} ({user_email})</p>
            <p><strong>Account:</strong> {account_name}</p>
            <p><strong>Requested Service:</strong> {service_name}</p>
        </body>
    </html>
    """

    message = Mail(from_email=from_email, to_emails=sales_rep_email, subject=email_subject, html_content=email_content)
    if user.get('email'):
        message.reply_to = user_email

    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        if response.status_code in [200, 201, 202]:
            return jsonify({'status': 'success', 'message': f'Access request submitted for {service_name}.'}), 200
        return jsonify({'status': 'error', 'message': 'Failed to deliver request email.'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500