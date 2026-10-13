from flask import Blueprint, render_template
from auth.fusionauth import login_required

partner_advantage_bp = Blueprint('partner_advantage', __name__)

@partner_advantage_bp.route('/partner-advantage')
@login_required
def partner_advantage(user):
    return render_template('partner_advantage/overview.html', user=user)

@partner_advantage_bp.route('/partner-advantage/tiers')
@login_required
def partner_advantage_tiers(user):
    return render_template('partner_advantage/tiers.html', user=user)

@partner_advantage_bp.route('/partner-advantage/drivers')
@login_required
def partner_advantage_drivers(user):
    return render_template('partner_advantage/drivers.html', user=user)

@partner_advantage_bp.route('/partner-advantage/portfolio')
@login_required
def partner_advantage_portfolio(user):
    return render_template('partner_advantage/portfolio.html', user=user)