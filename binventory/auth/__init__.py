"""Pluggable authorisation scaffold (backlog E4).

**Behaviour today is unchanged.** The default backend grants everything, so the
app works exactly as it did. The point is structural: decorating routes now,
while the refactor is already touching them, is cheap. Retrofitting permission
checks across ~50 routes later is not.

It also gives plugins something concrete to declare. A plugin manifest lists the
permissions it wants, and the user can see that before enabling it — the same
strings defined here.

To add real users later: implement one AuthBackend subclass, call
`set_backend()`, and add a login screen. No route bodies change.
"""
from .backend import (  # noqa: F401
    AuthBackend, NullAuthBackend, User, OWNER,
    current_user, get_backend, set_backend, has_permission, require,
    PERMISSIONS,
)
