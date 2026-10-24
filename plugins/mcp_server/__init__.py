"""AI Assistant (MCP server) plugin — lifecycle, access, and its panel in the Plugins tab.

The tools themselves live in server.py. This module runs them:

* **In-process, on its own port** (decision D0): the server shares the app's
  loaded CLIP model, analysis queue and event bus. It is a uvicorn thread
  serving the SDK's Streamable HTTP app, registered with `api.add_service`, so
  disabling the plugin closes the port.
* **This computer only, by default** (D1): bound to 127.0.0.1 with the SDK's
  DNS-rebinding guard on; no token needed, because only local programs can
  connect. "Allow other devices" binds to the LAN, always requires the access
  token, and serves HTTPS when the app has a certificate.
* **Docker:** a container's 127.0.0.1 isn't reachable from the host, so the
  image sets BINVENTORY_MCP_BIND=0.0.0.0 and compose publishes the port on the
  host's 127.0.0.1 only — the same "this computer only" guarantee.
* **One access token**, shown once when created and stored only as a SHA-256
  hash. Revoking it disconnects every device using it.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from binventory import config

_state = {"server": None, "thread": None, "url": "", "error": "", "lan": False, "tls": False}
_lock = threading.Lock()


# ── Access token ────────────────────────────────────────────────────────────

def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _token_info(api):
    return api.store.settings_get("token", None)          # {"hash", "created", "hint"}


def _make_verifier(api):
    from mcp.server.auth.provider import AccessToken, TokenVerifier

    class StoredTokenVerifier(TokenVerifier):
        """Accepts exactly the token created on the plugin's panel; scopes = the plugin's permissions."""
        async def verify_token(self, token):
            with api.app_context():
                info = _token_info(api)
            if not info or not hmac.compare_digest(info["hash"], _hash(token)):
                return None
            return AccessToken(token=token, client_id="binventory-owner",
                               scopes=list(api._info.permissions))
    return StoredTokenVerifier()


def _create_token(api):
    token = "binv_" + secrets.token_urlsafe(32)
    api.store.settings_set("token", {"hash": _hash(token), "hint": token[-4:],
                                     "created": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if api.setting("lan_access"):
        _restart(api)
    return {"message": "Access token created",
            "reveal": {"label": "Your access token — copy it now",
                       "help": "It won't be shown again. Paste it into your assistant's "
                               "connection settings (see the command above).",
                       "value": token}}


def _revoke_token(api):
    api.store.settings_set("token", None)
    _restart(api)
    return {"message": "Access token revoked"}


def _clear_activity(api):
    api.store.set("global", "", "activity", [])
    return {"message": "Activity cleared"}


# ── Server lifecycle ────────────────────────────────────────────────────────

def _hosts(port):
    import generate_cert
    hosts = ["127.0.0.1", "localhost"] + generate_cert.detect_local_ips()
    hosts += [h.strip() for h in os.environ.get("BINVENTORY_CERT_HOSTS", "").split(",") if h.strip()]
    return hosts


def _start(api):
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from . import server as mcp_server

    with _lock:
        _state.update(error="", url="")
        port = int(api.setting("port") or 5050)
        lan = bool(api.setting("lan_access"))
        cert, key = config.CERT_DIR / "cert.pem", config.CERT_DIR / "key.pem"
        tls = lan and cert.exists() and key.exists()
        scheme = "https" if tls else "http"

        verifier = auth = None
        if lan:
            if not _token_info(api):
                _state["error"] = ("Other devices need an access token. Press "
                                   "'Create access token', then it starts automatically.")
                return
            from pydantic import AnyHttpUrl
            from mcp.server.auth.settings import AuthSettings
            base = f"{scheme}://127.0.0.1:{port}"
            verifier = _make_verifier(api)
            # The token is our own (no external authorization server), so the
            # verifier checks it directly rather than validating an audience.
            auth = AuthSettings(issuer_url=AnyHttpUrl(base), resource_server_url=AnyHttpUrl(f"{base}/mcp"),
                                validate_token_resource=False)

        mcp = mcp_server.build(api, verifier, auth)
        hosts = _hosts(port) if lan else ["127.0.0.1", "localhost"]
        bind = "0.0.0.0" if lan else os.environ.get("BINVENTORY_MCP_BIND", "127.0.0.1")
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{h}:{port}" for h in hosts] + hosts,
            allowed_origins=[f"{s}://{h}:{port}" for h in hosts for s in ("http", "https")])
        app = mcp.streamable_http_app(host=bind, transport_security=security)

        server = uvicorn.Server(uvicorn.Config(
            app, host=bind, port=port, lifespan="on",
            log_level="warning", log_config=None,
            ssl_certfile=str(cert) if tls else None, ssl_keyfile=str(key) if tls else None))
        thread = threading.Thread(target=server.run, name="mcp-server", daemon=True)
        thread.start()
        for _ in range(100):                      # up to 5 s
            if server.started or not thread.is_alive():
                break
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            _state["error"] = f"Couldn't listen on port {port}. Is another program using it? Change the port below."
            return
        _state.update(server=server, thread=thread, lan=lan, tls=tls,
                      url=f"{scheme}://{'<this-computer-ip>' if lan else '127.0.0.1'}:{port}/mcp")
        api.log(f"listening on {scheme}://{bind}:{port}/mcp")


def _stop():
    with _lock:
        server, thread = _state["server"], _state["thread"]
        _state.update(server=None, thread=None, url="")
    if server:
        server.should_exit = True
    if thread:
        thread.join(5)


def _restart(api):
    _stop()
    _start(api)


# ── Plugins-tab panel ───────────────────────────────────────────────────────────

def _status(api):
    rows = []
    running = _state["server"] is not None
    port = int(api.setting("port") or 5050)
    if running:
        rows.append({"label": "Status", "value": "Running — " + (
            "any device on your network with the token can connect" if _state["lan"]
            else "only programs on this computer can connect")})
    else:
        rows.append({"label": "Status", "value": _state["error"] or "Not running"})

    if running:
        if _state["lan"]:
            import generate_cert
            ips = generate_cert.detect_local_ips() or ["<this-computer-ip>"]
            scheme = "https" if _state["tls"] else "http"
            url = f"{scheme}://{ips[0]}:{port}/mcp"
            rows.append({"label": "Connect Claude Code (on another computer)", "type": "code",
                         "value": f'claude mcp add --transport http binventory {url} '
                                  f'--header "Authorization: Bearer <your token>"'})
            if _state["tls"]:
                rows.append({"label": "The connection uses this app's own certificate. On that computer, trust it with",
                             "type": "code", "value": f"NODE_EXTRA_CA_CERTS={config.CERT_DIR / 'cert.pem'}"})
        else:
            rows.append({"label": "Connect Claude Code", "type": "code",
                         "value": f"claude mcp add --transport http binventory http://127.0.0.1:{port}/mcp"})
            rows.append({"label": "Other MCP clients", "type": "code", "value": f"http://127.0.0.1:{port}/mcp"})
    info = _token_info(api)
    rows.append({"label": "Access token", "value": f"…{info['hint']}, created {info['created'][:10]}"
                 if info else "None (only needed for other devices)"})
    rows.append({"label": "Changes", "value": "Allowed (never deletes)" if api.setting("allow_changes")
                 else "Off — the assistant can only look"})
    activity = api.store.get("global", "", "activity", []) or []
    rows.append({"label": "Recent assistant activity",
                 "value": [f"{_local_time(a['at'])}  {a['text']}" for a in activity[:8]]
                          or ["Nothing yet"]})
    return rows


def _local_time(stamp):
    """Activity is stored in UTC; show it on this computer's clock."""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return stamp[5:16].replace("T", " ")


# ── Entry point ─────────────────────────────────────────────────────────────

def register(api):
    api.add_service(lambda: _start(api), _stop, "MCP server")
    api.on_settings_changed(lambda changed: _restart(api) if {"port", "lan_access"} & set(changed) else None)
    api.add_action("create_token", lambda payload: _create_token(api))
    api.add_action("revoke_token", lambda payload: _revoke_token(api))
    api.add_action("clear_activity", lambda payload: _clear_activity(api))
    api.set_status(lambda: _status(api))
