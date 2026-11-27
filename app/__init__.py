import os
import secrets

from flask import Flask

from . import db


def create_app():
    app = Flask(__name__)
    db.init()
    key_path = os.path.join(db.DATA_DIR, "secret.key")
    if not os.path.exists(key_path):
        with open(key_path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(key_path) as f:
        app.secret_key = f.read().strip()
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    from .views import bp
    from . import supplies  # noqa: F401  비품 화면 (같은 bp에 등록)
    app.register_blueprint(bp)

    @app.teardown_appcontext
    def _close(exc):
        db.close()

    @app.template_filter("won")
    def won(v):
        try:
            return f"{round(v):,}원"
        except (TypeError, ValueError):
            return "—"

    @app.template_filter("num")
    def num(v, digits=0):
        if v is None:
            return "—"
        return f"{v:,.{digits}f}"

    return app
