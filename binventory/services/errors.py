"""Typed errors raised by services.

Services know nothing about HTTP; they raise one of these. The API layer maps
them to a status code in one error handler (`api.register`), and the MCP
server maps them to tool errors the model can read and act on. `extra` carries
machine-readable detail, e.g. `needs_bin=True` when a restock has no home.
"""


class ServiceError(Exception):
    status = 400

    def __init__(self, message, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class NotFound(ServiceError):
    status = 404


class Invalid(ServiceError):
    status = 400


class Conflict(ServiceError):
    status = 409


class Failed(ServiceError):
    status = 500


class Unavailable(ServiceError):
    status = 503


# Lets services write `UNSET` for "argument not given" where None is a value.
class _Unset:
    def __repr__(self):
        return "UNSET"

    def __bool__(self):
        return False


UNSET = _Unset()
