"""Phone access (Settings → 手机访问): a second web server for the same app on the local
network, so a phone on the same Wi-Fi can use it. Off by default.

Every request that doesn't come through the main loopback server needs the random key from
the QR code (?k=… once, then a cookie). Remote clients can chat and read notes, but not
change settings, log in, update or quit — those stay on the computer (see server.py).
"""
import hmac
import secrets
import socket
import sys
import threading

from . import settings

PORTS = range(8780, 8800)
COOKIE = "xhs_k"
_state = {"server": None, "thread": None, "port": None}
_lock = threading.Lock()
main_port = None  # the loopback server's port, set by xhs_reader.app


def conf():
    return settings.load()["mobile"]


def _save(**kw):
    s = settings.load()
    s["mobile"].update(kw)
    settings.save(s)


def lan_ip():
    """This computer's address on the local network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _free(port):
    with socket.socket() as s:
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def running_port():
    return _state["port"]


def start():
    """Start the network server if phone access is on (and it isn't running yet)."""
    import uvicorn
    from . import server
    with _lock:
        m = conf()
        if _state["server"] or not m["enabled"]:
            return
        # Reuse last time's port, so a phone's home-screen shortcut keeps working.
        port = m.get("port") if m.get("port") in PORTS and _free(m["port"]) else next((p for p in PORTS if _free(p)), None)
        if not port:
            raise RuntimeError("找不到可用的端口（8780–8799 都被占用了）")
        srv = uvicorn.Server(uvicorn.Config(server.app, host="0.0.0.0", port=port, log_level="warning", log_config=None))
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        _state.update(server=srv, thread=t, port=port)
        if m.get("port") != port:
            _save(port=port)


def stop():
    with _lock:
        srv, t = _state["server"], _state["thread"]
        if srv:
            srv.should_exit = True
            t.join(timeout=5)
        _state.update(server=None, thread=None, port=None)


def set_enabled(on):
    if on and not conf().get("token"):
        _save(token=secrets.token_urlsafe(18))
    _save(enabled=bool(on))
    start() if on else stop()


def reset_key():
    """New key: every phone has to scan the QR code again."""
    _save(token=secrets.token_urlsafe(18))


def url():
    ip, port, m = lan_ip(), _state["port"], conf()
    return f"http://{ip}:{port}/?k={m['token']}" if ip and port and m.get("token") else None


def is_remote(scope):
    """True for requests that didn't come through the main (loopback-only) server."""
    if main_port is None:  # not started by xhs_reader.app (tests)
        return False
    srv = scope.get("server")
    return not srv or srv[1] != main_port


def key_ok(value):
    m = conf()
    token = m.get("token") or ""
    return bool(m["enabled"] and token and value and hmac.compare_digest(str(value), token))


def qr_svg(text):
    import segno
    return segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
