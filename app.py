from dotenv import load_dotenv
load_dotenv()

import json
from flask import Flask

from src.store import init_db
from routes.dashboard import bp as dashboard_bp
from routes.proxy import bp as proxy_bp

init_db()

app = Flask(__name__)
app.jinja_env.filters["fromjson"] = json.loads
app.register_blueprint(dashboard_bp)
app.register_blueprint(proxy_bp)

if __name__ == "__main__":
    app.run(debug=True, port=5010)
