"""A tiny synchronous event bus (backlog G3).

Lets features react to things happening without the code that caused them
knowing who is listening. The three planned plugins all need this: an MCP
server watching for changes, a restocking integration reacting to consumption,
and any future notifier.

Deliberately simple:

* **Synchronous.** Handlers run inline. That keeps ordering obvious and avoids
  a second threading model; a handler that needs to be slow should hand off to
  the tagging queue itself.
* **Fail-soft.** A raising handler is logged and skipped. A misbehaving
  subscriber must never break the action that emitted the event — losing a
  notification is acceptable, losing an item is not.
* **Fire-and-forget.** Events are not persisted. If an audit trail
  is wanted later, that is itself just another subscriber.
"""
import threading
from collections import defaultdict

# Canonical event names. Kept as constants so typos fail loudly at import
# rather than silently subscribing to an event that is never emitted.
ITEM_CREATED       = "item.created"
ITEM_UPDATED       = "item.updated"
ITEM_DELETED       = "item.deleted"
ITEM_TAGGED        = "item.tagged"
ITEM_CHECKED_OUT   = "item.checked_out"
ITEM_RETURNED      = "item.returned"
ITEM_CONSUMED      = "item.consumed"
ITEM_RESTOCKED     = "item.restocked"
BIN_CREATED        = "bin.created"
BIN_UPDATED        = "bin.updated"
BIN_DELETED        = "bin.deleted"
BULK_SESSION_ENDED = "bulk_session.ended"

KNOWN_EVENTS = {v for k, v in list(globals().items()) if k.isupper() and isinstance(v, str)}

_subscribers = defaultdict(list)
_lock = threading.Lock()


def on(event, handler):
    """Subscribe to an event. Returns an unsubscribe callable."""
    if event not in KNOWN_EVENTS:
        print(f"[events] Warning: subscribing to unknown event {event!r}")
    with _lock:
        _subscribers[event].append(handler)

    def off():
        with _lock:
            if handler in _subscribers[event]:
                _subscribers[event].remove(handler)
    return off


def emit(event, **payload):
    """Notify every subscriber. Never raises."""
    with _lock:
        handlers = list(_subscribers.get(event, ()))
    for h in handlers:
        try:
            h(event=event, **payload)
        except Exception as e:
            # One bad subscriber must not break the caller's operation.
            print(f"[events] handler for {event} failed: {e}")


def subscribers(event=None):
    """Introspection for diagnostics and tests."""
    with _lock:
        if event:
            return list(_subscribers.get(event, ()))
        return {k: list(v) for k, v in _subscribers.items() if v}


def clear():
    """Drop all subscribers (tests only)."""
    with _lock:
        _subscribers.clear()
