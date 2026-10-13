import os
import json
import requests
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, current_app
from auth.fusionauth import login_required
from extensions import cache
from integrations.zoho_crm import get_cached_rewards, get_cached_reward_requests, get_cached_claims
from datetime import date, timedelta

perks_bp = Blueprint('perks', __name__)

@perks_bp.route('/options')
@login_required
def perks_options(user):
    if 'Partner Perks' not in user.get('authorized_apps', []):
        return redirect(url_for('main.dashboard'))

    cards_path = os.path.join(current_app.root_path, 'cards.json')
    brands, catalog_name = [], "Reward Link Options"

    if os.path.exists(cards_path):
        try:
            with open(cards_path, 'r', encoding='utf-8') as f:
                cards_data = json.load(f)
                catalog_name = cards_data.get('catalogName', catalog_name)
                brands = cards_data.get('brands', [])
        except Exception as e:
            print(f"Error reading cards.json: {e}")

    return render_template('perks/options.html', catalog_name=catalog_name, brands=brands, user=user)

@perks_bp.route('/home')
@login_required
def perks_home(user):
    if 'Partner Perks' not in user.get('authorized_apps', []):
        return redirect(url_for('main.dashboard'))
    return render_template('perks/home.html', user=user)

@perks_bp.route('/rules')
@login_required
def perks_rules(user):
    if 'Partner Perks' not in user.get('authorized_apps', []):
        return redirect(url_for('main.dashboard'))
    return render_template('perks/rules.html', user=user)

@perks_bp.route('/terms')
@login_required
def perks_terms(user):
    if 'Partner Perks' not in user.get('authorized_apps', []):
        return redirect(url_for('main.dashboard'))
    return render_template('perks/terms.html', user=user)

@perks_bp.route('/redeem', methods=['POST'])
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

        cache.delete_memoized(get_cached_rewards, user_email)
        cache.delete_memoized(get_cached_reward_requests, user_email)

        if res_data.get('code') == 'success':
            output_str = res_data.get('details', {}).get('output', '{}')
            parsed_output = json.loads(output_str) if isinstance(output_str, str) else output_str

            if 'Status' in parsed_output and 'Error' in parsed_output['Status']:
                return jsonify({'status': 'error', 'message': parsed_output['Status']}), 400

            return jsonify({'status': 'success', 'data': parsed_output}), 200

        return jsonify({'status': 'error', 'message': res_data.get('message', 'Failed.')}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@perks_bp.route('/rewards')
@login_required
def perks_rewards(user):
    if 'Partner Perks' not in user.get('authorized_apps', []):
        return redirect(url_for('main.dashboard'))
        
    api_email = user.get('email')

    try:
        if request.args.get('refresh') == 'true':
            cache.delete_memoized(get_cached_rewards, api_email)
            cache.delete_memoized(get_cached_reward_requests, api_email)

        data = get_cached_rewards(api_email)
        points, rewards_user = 0, ""
        
        if data.get("code") == "success" and "details" in data and "output" in data["details"]:
            parsed_data = json.loads(data["details"]["output"]) if isinstance(data["details"]["output"], str) else data["details"]["output"]
            points = parsed_data.get("Points", 0)
            rewards_user = parsed_data.get("UserName", "")

        req_data = get_cached_reward_requests(api_email)
        reward_requests = []

        if req_data.get("code") == "success" and "details" in req_data and "output" in req_data["details"]:
            parsed_req_data = json.loads(req_data["details"]["output"]) if isinstance(req_data["details"]["output"], str) else req_data["details"]["output"]
            if isinstance(parsed_req_data, dict):
                reward_requests = parsed_req_data.get("Claims", parsed_req_data.get("Reward_Requests", parsed_req_data.get("requests", [])))
            elif isinstance(parsed_req_data, list):
                reward_requests = parsed_req_data
            
        return render_template('perks/rewards.html', points=points, rewards_user=rewards_user, reward_requests=reward_requests, user=user)
    except Exception as e:
        return f"Error fetching rewards data: {str(e)}", 500

@perks_bp.route('/claim', methods=['GET', 'POST'])
@login_required
def perks_claim(user):
    if 'Partner Perks' not in user.get('authorized_apps', []):
        return redirect(url_for('main.dashboard'))
    today = date.today()
    min_date = today + timedelta(days=-90)
    return render_template('perks/claim.html', user=user, max_date=today.strftime("%Y-%m-%d"), min_date=min_date.strftime("%Y-%m-%d"))

@perks_bp.route('/claim/review')
@login_required
def perk_claim_review(user):
  # Add your review logic here
  return render_template('perks/claim_review.html', user=user)

@perks_bp.route('/claims')
@login_required
def perks_claims(user):
    if 'Partner Perks' not in user.get('authorized_apps', []):
        return redirect(url_for('main.dashboard'))
        
    api_email = user.get('email')

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
            claims, summary = data.get("Claims", []), {}
            
        return render_template('perks/claims.html', claims=claims, summary=summary, user=user)
    except Exception as e:
        return f"Error fetching claims data: {str(e)}", 500