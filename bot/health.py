"""
Health-check server + self-ping loop for Render free tier.

How Render's sleep works
------------------------
Render puts a free web service to sleep when its *reverse proxy* sees no
inbound HTTP traffic for 15 minutes.  A Telegram bot uses outbound polling
(no inbound port needed) so it can keep chatting on Telegram while the
service appears "inactive" to Render — and gets killed.

Two-part solution
-----------------
1. ThreadingHTTPServer bound to $PORT  →  Render sees a live web service and
   UptimeRobot can ping /health for external monitoring.

2. Self-ping coroutine  →  every 4 minutes the process makes an HTTPS request
   to its own RENDER_EXTERNAL_URL.  That traffic goes through Render's reverse
   proxy, counts as inbound activity, and keeps the service awake even if
   UptimeRobot backs off its check interval.

No extra dependencies — stdlib only (http.client, urllib.parse, asyncio).
"""

import asyncio
import http.client
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn


# ─────────────────────────────────────────
#  HTTP server
# ─────────────────────────────────────────

class _ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    """Each ping is handled in its own thread — never blocked by the event loop."""
    allow_reuse_address = True
    daemon_threads = True


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def start_health_server() -> None:
    """
    Bind to $PORT (Render injects this) and start serving in a daemon thread.
    Call this before starting the Telegram bot's event loop.
    """
    port   = int(os.getenv("PORT", "8080"))
    server = _ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[health] Server listening on port {port}")


# ─────────────────────────────────────────
#  Self-ping keep-alive
# ─────────────────────────────────────────

_SELF_PING_INTERVAL = 4 * 60  # 4 minutes — well under Render's 15-min sleep threshold


async def self_ping_loop() -> None:
    """
    Ping our own public URL every 4 minutes so Render's proxy sees inbound
    traffic and never triggers the 15-minute sleep timer.

    Only active when RENDER_EXTERNAL_URL is set (i.e., running on Render).
    Safe to schedule unconditionally — exits immediately if not on Render.
    """
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        return  # not on Render — nothing to do

    parsed = urllib.parse.urlparse(render_url)
    host   = parsed.netloc
    use_https = parsed.scheme == "https"

    print(f"[health] Self-ping active → {render_url}/health every {_SELF_PING_INTERVAL // 60} min")

    # Let the bot finish starting up before the first ping
    await asyncio.sleep(90)

    loop = asyncio.get_event_loop()
    while True:
        try:
            def _ping() -> int:
                cls  = http.client.HTTPSConnection if use_https else http.client.HTTPConnection
                conn = cls(host, timeout=10)
                conn.request("GET", "/health")
                status = conn.getresponse().status
                conn.close()
                return status

            status = await loop.run_in_executor(None, _ping)
            print(f"[health] self-ping → {status}")
        except Exception as exc:
            print(f"[health] self-ping failed: {exc}")

        await asyncio.sleep(_SELF_PING_INTERVAL)
