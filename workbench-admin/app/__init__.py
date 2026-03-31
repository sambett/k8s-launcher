"""
app/__init__.py — Application factory.
Adding a new feature = import its blueprint and register it here.
"""

from flask import Flask, render_template
from app.auth import require_auth


def create_app():
    app = Flask(__name__, template_folder="templates")

    from app.routes.profiles import bp as profiles_bp
    from app.routes.registry import bp as registry_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.images   import bp as images_bp
    from app.routes.groups   import bp as groups_bp

    app.register_blueprint(profiles_bp)
    app.register_blueprint(registry_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(images_bp)
    app.register_blueprint(groups_bp)

    @app.route("/")
    @require_auth
    def index():
        return render_template("index.html")

    return app
