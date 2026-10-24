"""Application settings access."""
from ..db import db
from ..models import AppSettings

def get_settings():
    """Fetch (creating if needed) the single AppSettings row.

    Must be called inside an app context. Centralising this means every caller
    gets the same defaults and we never have to null-check the row.
    """
    s = AppSettings.query.first()
    if not s:
        s = AppSettings()
        db.session.add(s)
        db.session.commit()
    return s
