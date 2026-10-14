"""GM commands over the core's SOAP interface; the server log over ssh."""
import re
import subprocess
from xml.sax.saxutils import escape

import requests

ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="urn:MaNGOS">'
    "<SOAP-ENV:Body><ns1:executeCommand><command>{cmd}</command></ns1:executeCommand></SOAP-ENV:Body>"
    "</SOAP-ENV:Envelope>"
)


class Server:
    def __init__(self, host, port, user, password, container, ssh=(), post=requests.post, run=subprocess.run):
        self.url, self.auth, self.container = f"http://{host}:{port}/", (user, password), container
        self.ssh = list(ssh)
        self._post, self._run = post, run

    def gm(self, cmd: str) -> str:
        body = ENVELOPE.format(cmd=escape(cmd)).encode("utf-8")
        try:
            r = self._post(self.url, data=body, auth=self.auth, headers={"Content-Type": "text/xml"}, timeout=15)
        except requests.RequestException as err:
            return f"SOAP error: {err}"
        if r.status_code != 200:
            m = re.search(r"<faultstring>(.*?)</faultstring>", r.text, re.S)
            return f"SOAP error: {m.group(1).strip() if m else r.text[:200]}"
        m = re.search(r"<result>(.*?)</result>", r.text, re.S)
        return m.group(1) if m else ""

    def log_since(self, seconds: int) -> str:
        if not self.ssh:
            return "server log not configured (server_ssh missing in settings)"
        p = self._run(["ssh", "-o", "BatchMode=yes", *self.ssh, "docker", "logs", "--since", f"{seconds}s", self.container],
                      capture_output=True, text=True, timeout=30)
        return p.stdout + p.stderr
