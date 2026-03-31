"""
dashboard.py — Entry point only.
Imports the app factory and starts the server.
Nothing else belongs here.
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8888, debug=False)
