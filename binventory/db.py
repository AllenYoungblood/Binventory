"""The SQLAlchemy instance.

Created without an app so models can import it at module level without a
circular dependency; the application factory calls `db.init_app(app)` later.
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
