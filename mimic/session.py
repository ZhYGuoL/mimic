"""The runtime every generated client is built on.

A Session holds your captured auth and makes calls that look like they came
from the real app. Generated clients subclass App and call self.get/.post.

    from hinge_client import Hinge
    acc = Hinge()          # auto-pulls your captured auth from mitmweb
    acc.get_recs()
"""
import json
import os

import requests

from . import extract
from .sources.mitm import Mitm


class Session:
    """Base URL + reusable headers, with helpers that return parsed JSON.

    Construct one of four ways:
        Session.from_mitm("prod-api.hingeaws.net")   # pull from mitmweb
        Session(base_url=..., headers={...})         # explicit
        Session.from_curl(text)                      # paste a copied cURL
        Session.load("session.json")                 # restore a saved session
    """

    def __init__(self, base_url, headers=None, host=None, mitm=None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.host = host
        self._mitm = mitm  # kept for token refresh
        self._http = requests.Session()

    # ---- constructors -------------------------------------------------------
    @classmethod
    def from_mitm(cls, host, mitm=None):
        mitm = mitm or Mitm()
        flows = mitm.flows()
        headers = extract.session_headers(flows, host)
        if not headers:
            raise RuntimeError(
                f"no authenticated request to {host} captured yet — "
                f"open the app once so mitmweb sees a real call, then retry"
            )
        return cls(extract.base_url(flows, host), headers, host=host, mitm=mitm)

    @classmethod
    def from_curl(cls, text):
        base_url, headers = _parse_curl(text)
        return cls(base_url, headers)

    # ---- persistence --------------------------------------------------------
    def save(self, path):
        """Persist this session's auth to a JSON file for later reuse.

        Writes base_url, headers, and host so a loaded session makes the same
        authed calls with no mitmweb running. The runtime bits (the mitm
        backend, the requests session) are left out on purpose. The file holds
        live tokens, so it's written 0600.
        """
        data = {
            "base_url": self.base_url,
            "headers": self.headers,
            "host": self.host,
            "version": 1,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(path, 0o600)

    @classmethod
    def load(cls, path):
        """Restore a session written by save().

        The loaded session has no mitm backend, so a 401/403 won't auto-refresh
        (there's no proxy to pull a fresh token from); it just returns the
        response like any other call.
        """
        with open(path) as f:
            data = json.load(f)
        return cls(data["base_url"], data.get("headers", {}), host=data.get("host"))

    # ---- calls --------------------------------------------------------------
    def request(self, method, path, json=None, params=None, refresh=True, **kw):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        r = self._http.request(
            method, url, headers=self.headers, json=json, params=params, **kw
        )
        if r.status_code in (401, 403) and refresh and self.host and self._mitm:
            # token likely rotated — re-pull from mitm once and retry
            self.headers = extract.session_headers(self._mitm.flows(), self.host)
            return self.request(
                method, path, json=json, params=params, refresh=False, **kw
            )
        return _parse(r)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)


class App(Session):
    """Subclass this in a generated client and add named methods.

    class Hinge(App):
        HOST = "prod-api.hingeaws.net"
        def get_recs(self):
            return self.post("/rec/v2", {"playerId": self.player_id})
    """

    HOST = None

    def __init__(self, **kw):
        if not kw and self.HOST:
            got = Session.from_mitm(self.HOST)
            super().__init__(got.base_url, got.headers, host=got.host, mitm=got._mitm)
        else:
            super().__init__(**kw)


def _parse(r):
    try:
        return r.json()
    except ValueError:
        return r.text


def _parse_curl(text):
    """Minimal `curl 'URL' -H 'k: v' ...` parser for the paste fallback."""
    import shlex
    from urllib.parse import urlsplit

    tokens = shlex.split(text.replace("\\\n", " "))
    url, headers = None, {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            i += 1
            k, _, v = tokens[i].partition(":")
            headers[k.strip()] = v.strip()
        elif t.startswith("http"):
            url = t
        i += 1
    if not url:
        raise ValueError("no URL found in cURL text")
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", headers
