"""Single-owner bearer/session authentication and browser origin boundary."""
import ipaddress
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse


def is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Security:
    def __init__(self, host="127.0.0.1", token=None, allowed_hosts=None):
        if not is_loopback(host) and (not token or len(token) < 24):
            raise ValueError("Non-loopback binding requires STUDIO_WEB_TOKEN of at least 24 characters")
        self.host = host
        self.token = token
        self.sessions = {}
        configured = (allowed_hosts if allowed_hosts is not None
                      else os.environ.get("STUDIO_WEB_ALLOWED_HOSTS", "").split(","))
        if isinstance(configured, str):
            configured = configured.split(",")
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        for value in configured:
            value = value.strip().lower().strip("[]")
            if value:
                if any(c in value for c in "/*:@ "):
                    raise ValueError("Allowed hosts must be exact hostnames")
                self.allowed_hosts.add(value)

    def authenticate(self, request):
        if not self.token:
            return True
        bearer = request.headers.get("authorization", "")
        if bearer.startswith("Bearer "):
            return secrets.compare_digest(bearer[7:].encode(), self.token.encode())
        session = request.cookies.get("studio_session", "")
        return self.sessions.get(session, 0) > time.monotonic()

    def session(self, token):
        if self.token and not secrets.compare_digest(token.encode(), self.token.encode()):
            return None
        self.sessions = {k: v for k, v in self.sessions.items() if v > time.monotonic()}
        identifier = secrets.token_urlsafe(32)
        self.sessions[identifier] = time.monotonic() + 43200
        return identifier

    async def middleware(self, request, call_next):
        hostname = request.url.hostname or ""
        # Exact Host allowlist applies even with a valid bearer token.
        if hostname.lower() not in self.allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                expected = urlsplit(str(request.base_url))
                if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
                    return JSONResponse({"detail": "Foreign Origin is forbidden"}, status_code=403)
            elif request.cookies.get("studio_session") and not request.headers.get("authorization"):
                return JSONResponse({"detail": "Session mutations require same-origin Origin"},
                                    status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request forbidden"}, status_code=403)
        public = request.url.path in {"/api/health", "/api/session"}
        if request.url.path.startswith("/api/") and not public and not self.authenticate(request):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response
