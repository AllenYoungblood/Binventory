"""Authorisation backends and the `require` decorator."""
import functools

from flask import jsonify

# ── Permission vocabulary ─────────────────────────────────────────────────────
# Defined up front so plugins can declare what they need in their manifest and
# the user can review it before enabling. Coarse on purpose: finer granularity
# is easy to add later, hard to remove once anything depends on it.
PERMISSIONS = {
    "bins:read", "bins:write",
    "items:read", "items:write",
    "search:read",
    "vocab:read", "vocab:write",
    "settings:read", "settings:write",
    "lighting:read", "lighting:control",
    "maintenance:run",
    # Declared by plugins that contact services outside the LAN. Shown to the
    # user before enabling; not enforceable in-process, so it is a disclosure.
    "network:external",
    # Declared by plugins that open a port and accept connections (the MCP
    # server). Also a disclosure.
    "network:listen",
    # Declared by plugins that serve their own pages or endpoints under
    # /p/<plugin id>/ (the label maker).
    "web:routes",
}


class User:
    """A minimal principal. Real backends may subclass with more detail."""

    def __init__(self, id="owner", name="Owner", permissions=None):
        self.id = id
        self.name = name
        # None means "all permissions" — the single-owner default.
        self.permissions = permissions

    def can(self, permission):
        return self.permissions is None or permission in self.permissions

    def __repr__(self):
        return f"<User {self.id}>"


# The implicit single user this app is built for.
OWNER = User()


class AuthBackend:
    """Interface every backend implements."""

    def current_user(self):
        raise NotImplementedError

    def has_permission(self, user, permission):
        raise NotImplementedError


class NullAuthBackend(AuthBackend):
    """The default: one implicit owner who can do everything.

    This is the honest model for a single-user LAN app, and keeps the decorators
    a no-op until someone actually wants multi-user.
    """

    def current_user(self):
        return OWNER

    def has_permission(self, user, permission):
        return True


_backend = NullAuthBackend()


def get_backend():
    return _backend


def set_backend(backend):
    """Swap in a real backend. The only call site needed to enable auth."""
    global _backend
    if not isinstance(backend, AuthBackend):
        raise TypeError("backend must subclass AuthBackend")
    _backend = backend
    return _backend


def current_user():
    return _backend.current_user()


def has_permission(permission):
    return _backend.has_permission(_backend.current_user(), permission)


def require(permission):
    """Route decorator asserting a permission.

    With NullAuthBackend this always passes, so adding it changes nothing today.
    Unknown permission strings raise at import time — a typo should be a startup
    failure, not a silently-open endpoint.
    """
    if permission not in PERMISSIONS:
        raise ValueError(f"Unknown permission {permission!r}; add it to auth.PERMISSIONS")

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not has_permission(permission):
                return jsonify({"error": "Not permitted", "permission": permission}), 403
            return fn(*args, **kwargs)
        wrapper._required_permission = permission
        return wrapper
    return decorator
