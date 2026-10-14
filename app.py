import os
from flask import Flask, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from extensions import cache, oauth

# Import Blueprints
from auth.routes import auth_bp
from routes.main import main_bp
from routes.perks import perks_bp
from routes.support import support_bp
from routes.sales import sales_bp
from routes.employees import employees_bp
from routes.partner_advantage import partner_advantage_bp

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    @app.before_request
    def make_session_permanent():
        from flask import session
        session.permanent = True

    cache.init_app(app)
    oauth.init_app(app)

    oauth.register(
        name='fusionauth',
        client_id=os.environ.get('FUSIONAUTH_CLIENT_ID'),
        client_secret=os.environ.get('FUSIONAUTH_CLIENT_SECRET'),
        server_metadata_url=f"{os.environ.get('FUSIONAUTH_URL')}/.well-known/openid-configuration",
        client_kwargs={'scope': 'openid profile email offline_access'}
    )


    @app.route('/sw.js')
    def service_worker():
       return send_from_directory('static', 'sw.js', mimetype='application/javascript')
    # Register Blueprints with Prefixes
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(perks_bp, url_prefix='/perks')
    app.register_blueprint(support_bp)
    app.register_blueprint(sales_bp, url_prefix='/sales')
    app.register_blueprint(employees_bp)
    app.register_blueprint(partner_advantage_bp)

    return app

app = create_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)