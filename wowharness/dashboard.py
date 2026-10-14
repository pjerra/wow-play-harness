"""Client for the TortoiseBots observability dashboard and the per-bot decision timeline."""
import requests


class DashboardUnavailable(Exception):
    pass


class Dashboard:
    def __init__(self, base_url: str, user: str, password: str, session=None):
        self.base = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.session = session if session is not None else requests.Session()

    def login(self) -> None:
        try:
            r = self.session.post(f"{self.base}/api/v1/auth/login",
                                  json={"username": self.user, "password": self.password}, timeout=10)
        except Exception as err:
            raise DashboardUnavailable(f"dashboard unreachable: {type(err).__name__}") from None
        if not 200 <= r.status_code < 300:
            text = (r.text or "").replace(self.password, "***") if self.password else (r.text or "")
            raise DashboardUnavailable(f"dashboard login failed: {r.status_code} {text[:200]}")

    def _fetch(self, path: str):
        try:
            return self.session.get(f"{self.base}{path}", timeout=10)
        except Exception as err:
            raise DashboardUnavailable(f"dashboard unreachable: {type(err).__name__}") from None

    def _get(self, path: str):
        r = self._fetch(path)
        if r.status_code == 401:
            self.login()
            r = self._fetch(path)
        if not 200 <= r.status_code < 300:
            raise DashboardUnavailable(f"dashboard GET {path} failed: {r.status_code} {(r.text or '')[:200]}")
        try:
            return r.json()
        except ValueError:
            raise DashboardUnavailable(f"dashboard GET {path} returned non-JSON: {(r.text or '')[:60]!r}") from None

    def bots(self, names: list[str] | None = None) -> list[dict]:
        data = self._get("/api/v1/bots")
        if isinstance(data, dict):
            data = data.get("bots") or []
        if names:
            wanted = {n.lower() for n in names}
            data = [b for b in data if str(b.get("name", "")).lower() in wanted]
        return data

    def issues(self):
        return self._get("/api/v1/issues")

    def anomalies(self):
        return self._get("/api/v1/anomalies")


_WATCHED = ("last_action", "last_trigger", "target", "state")


def decision_timeline(samples: list[tuple[float, list[dict]]], names: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {n: [] for n in names}
    last: dict[str, tuple] = {}
    for t, bots in samples:
        by_name = {str(b.get("name", "")).lower(): b for b in bots}
        for n in names:
            b = by_name.get(n.lower())
            if b is None:
                continue
            key = tuple(b.get(f) for f in _WATCHED)
            if last.get(n) == key:
                continue
            last[n] = key
            mhp = b.get("max_hp") or 0
            hp_pct = round(100 * (b.get("hp") or 0) / mhp) if mhp else 0
            out[n].append({"t": t, "last_action": b.get("last_action"), "last_trigger": b.get("last_trigger"),
                           "target": b.get("target"), "state": b.get("state"), "hp_pct": hp_pct})
    return out
