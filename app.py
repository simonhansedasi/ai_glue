from dotenv import load_dotenv
load_dotenv()

import hmac
import json
import os
from flask import Flask, Response, request

from src.store import init_db
from routes.dashboard import bp as dashboard_bp
from routes.proxy import bp as proxy_bp
from routes.evals import bp as evals_bp
from routes.staging import bp as staging_bp

init_db()

app = Flask(__name__)
app.jinja_env.filters["fromjson"] = json.loads
app.register_blueprint(dashboard_bp)
app.register_blueprint(proxy_bp)
app.register_blueprint(evals_bp)
app.register_blueprint(staging_bp)



@app.before_request
def require_token():
    """HTTP Basic auth with AIGLUE_TOKEN as the password (any username), on everything except /proxy/*.

    ponytail: unset token = open, so existing instances keep working until each is configured.
    Plain HTTP between instances, so the token is readable on the wire; add TLS if that matters.
    /proxy/* is exempt on purpose: apps and Claude Code authenticate with their own provider keys.
    """
    token = os.getenv("AIGLUE_TOKEN")
    if not token or request.path.startswith("/proxy/"):
        return None
    given = request.authorization.password if request.authorization and request.authorization.password else ""
    if hmac.compare_digest(given.encode(), token.encode()):
        return None
    return Response("authentication required", 401, {"WWW-Authenticate": 'Basic realm="ai_glue"'})


if __name__ == "__main__":
    app.run(debug=True, port=5010)
