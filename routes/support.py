import os
import json
import requests
from flask import Blueprint, render_template, request, jsonify, session, current_app
from auth.fusionauth import login_required
from extensions import cache
from integrations.zoho_desk import vf_create_ticket, zoho_auth
from integrations.zoho_crm import get_cached_shipment_tracking, get_cached_open_installs

support_bp = Blueprint('support', __name__)

@cache.memoize(timeout=600)
def get_reseller_open_tickets(account_name: str) -> list:
    if not account_name:
        return []

    url = "https://desk.zoho.com/api/v1/tickets/search"
    try:
        token = zoho_auth.get_access_token()
    except Exception as ex:
        current_app.logger.error(f"Failed to fetch Zoho access token: {ex}")
        return []

    headers = {
        "orgId": os.environ.get("ZOHO_ORG_ID", "7868846"),
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json",
    }

    params = {
        "from": 0, "limit": 100, "departmentId": "20427000000006907",
        "status": "Open,Awaiting Development,Awaiting Customer Response,On Hold,Pending,Resolved,Scheduled",
        "customField1": f"cf_reseller_account_name:{account_name}",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        tickets = data.get("data", []) if isinstance(data, dict) else data
        return tickets if isinstance(tickets, list) else []
    except Exception as ex:
        current_app.logger.error(f"Error searching reseller tickets in Zoho Desk: {ex}")
        return []

@support_bp.route('/support/tools')
@login_required
def support_tools(user):
    return render_template('support/support_tools.html', user=user)

@support_bp.route('/support/create-ticket', methods=['GET', 'POST'])
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

        if not ticket_subject or not description or not product_involved:
            return jsonify({'status': 'error', 'message': 'Subject, Product Involved, and Description are required.'}), 400

        account_category = (user.get('account_category') or '').strip().lower()
        reseller_account_name = ""
        if account_category in ['internal', 'reseller']:
            reseller_account_name = user.get('account') or user.get('reseller_account', '')

        ticket_number = vf_create_ticket(
            ticket_subject=ticket_subject, firstname=first_name, lastname=last_name,
            email=email, description=description, product_involved=product_involved,
            crn=crn, reseller_account_name=reseller_account_name
        )

        if ticket_number != "0":
            return jsonify({'status': 'success', 'message': f'Support ticket #{ticket_number} created successfully!', 'ticket_number': ticket_number}), 200
        else:
            return jsonify({'status': 'error', 'message': 'Failed to create support ticket in Zoho Desk.'}), 500

    return render_template('/support/create_ticket.html', user=user, default_crn=request.args.get('crn', ''))

@support_bp.route('/support/open-tickets')
@login_required
def open_tickets(user):
    account_category = (user.get('account_category') or '').strip().lower()
    account_name = (user.get('account') or user.get('reseller_account') or '').strip()
    tickets = []

    if account_category == 'reseller' and account_name:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_reseller_open_tickets, account_name)
        tickets = get_reseller_open_tickets(account_name)

    return render_template('support/open_tickets.html', user=user, tickets=tickets, total_tickets=len(tickets), account_name=account_name)


@support_bp.route('/support/open-installs')
@login_required
def open_installs(user):
    user_email = user.get('email', '')
    api_email = user_email
    if not user_email.lower().endswith('@acd-inc.com'):
        return "Unauthorized: Open Installs lookup is restricted to internal ACDI employees.", 403
    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_open_installs, api_email)

        data = get_cached_open_installs(api_email)

        if data.get("code") == "success":
            raw_output = data.get("details", {}).get("output", "[]")
            parsed_installs = json.loads(raw_output) if isinstance(raw_output, str) and raw_output.strip() else (raw_output if isinstance(raw_output, list) else [])
            return render_template('support/open_installs.html', user=user, installs=parsed_installs, total_installs=len(parsed_installs))
        else:
            return "API returned an error loading open installs.", 400
    except Exception as e:
        return f"Error fetching open installs data: {str(e)}", 500

@support_bp.route('/suppport/shipment-tracking')
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