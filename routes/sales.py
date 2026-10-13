import json
from flask import Blueprint, render_template, request, session, redirect, url_for, current_app
from auth.fusionauth import login_required
from extensions import cache
from integrations.zoho_crm import (
    get_or_update_reseller_accounts, 
    refresh_reseller_accounts_gcs, 
    get_cached_account_contacts, 
    get_cached_licenses, 
    get_cached_open_deals
)

sales_bp = Blueprint('sales', __name__)

@sales_bp.route('/tools')
@login_required
def sales_tools(user):
    return render_template('sales/sales_tools.html', user=user)

@sales_bp.route('/accounts')
@login_required
def rep_accounts(user):
    user_email = user.get('email', '')
    permissions = user.get('permissions', [])
    is_admin = 'Admin' in permissions or 'original_admin_user' in session

    try:
        if request.args.get('refresh') == 'true':
            data = refresh_reseller_accounts_gcs()
        else:
            data = get_or_update_reseller_accounts()

        raw_accounts = data.get("Accounts", [])
        accounts = raw_accounts if is_admin else [
            acc for acc in raw_accounts if acc.get('Owner.email', '').lower() == user_email.lower()
        ]

        return render_template('sales/rep_accounts.html', user=user, accounts=accounts, total_accounts=len(accounts))
    except Exception as e:
        current_app.logger.error(f"Error loading rep accounts: {e}")
        return f"Error fetching reseller accounts: {str(e)}", 500

@sales_bp.route('/accounts/<account_id>/contacts')
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
            contacts = parsed_data.get("Contacts") or parsed_data.get("data") or []
            return render_template(
                'account_contacts.html', 
                user=user, 
                contacts=contacts, 
                total_contacts=len(contacts), 
                account_id=account_id, 
                account_name=account_name, 
                is_admin=is_admin
            )
        else:
            return "API returned an error loading contacts.", 400
    except Exception as e:
        return f"Error fetching contacts data: {str(e)}", 500

@sales_bp.route('/set_account/<account_id>')
@login_required
def set_active_account(user, account_id):
    session['active_account_id'] = account_id
    return redirect(url_for('sales.reseller_licenses'))

@sales_bp.route('/licenses')
@login_required
def reseller_licenses(user):
    api_email = user.get('email')
    account_id = session.get('active_account_id')
    print(account_id)
    if account_id is None: 
        return redirect(url_for('sales.rep_accounts'))
    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_licenses, api_email, account_id)
            
        data = get_cached_licenses(api_email, account_id)
        if data.get("code") == "success":
            parsed_data = json.loads(data["details"]["output"])
            return render_template('sales/licenses.html', user=user, licenses=parsed_data.get("Licenses", []), summary=parsed_data.get("Summary", {}))
        else:
            return "API returned an error.", 400
    except Exception as e:
        return f"Error fetching data: {str(e)}", 500

@sales_bp.route('/deals')
@login_required
def open_deals(user):
    api_email = user.get('email')
    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_open_deals, api_email)
            
        data = get_cached_open_deals(api_email)
        if data.get("code") == "success":
            parsed_data = json.loads(data["details"]["output"])
            return render_template('sales/open_deals.html', user=user, deals=parsed_data.get("Deals", []), total_deals=parsed_data.get("Total_Number_of_Open_Deals", 0))
        else:
            return "API returned an error.", 400
    except Exception as e:
        return f"Error fetching data: {str(e)}", 500

@sales_bp.route('/sales/shipment-tracking')
@login_required
def shipment_tracking(user):
    account_id = session.get('active_account_id') or user.get('account_id', '')
    user_email = user.get('email', '')

    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_shipment_tracking, account_id)

        data = get_cached_shipment_tracking(account_id)
        if data.get("code") == "success":
            raw_output = data.get("details", {}).get("output", "{}")
            parsed_data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            shipments = parsed_data.get("Shipments", [])
            return render_template('support/shipment_tracking.html', user=user, shipments=shipments, total_shipments=len(shipments))
        else:
            return "API returned an error.", 400
    except Exception as e:
        return f"Error fetching shipment data: {str(e)}", 500
