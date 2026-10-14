"""Settings and secrets from ~/.wowharness.json or $WOWHARNESS_CONFIG (never in the repo)."""
import json
import os
from dataclasses import dataclass

DEFAULT_PATH = os.environ.get("WOWHARNESS_CONFIG") or os.path.join(os.path.expanduser("~"), ".wowharness.json")


@dataclass(frozen=True)
class Config:
    client_dir: str
    server_host: str
    soap_port: int
    soap_user: str
    soap_password: str
    account: str
    password: str
    login_account_xy: tuple[float, float]
    login_password_xy: tuple[float, float]
    server_container: str
    dashboard_url: str = ""
    dashboard_user: str = ""
    dashboard_password: str = ""
    server_ssh: tuple[str, ...] = ()
    server_mmaps: str = "~/tortoise-wow-server/data/mmaps"  # walk_to reads tiles from here (ssh, read-only)
    mmaps_cache: str = ""  # local copy of the tiles walk_to used; "" = ~/.wowharness-mmaps


def load_config(path: str | None = None) -> Config:
    with open(path or DEFAULT_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return Config(
        client_dir=raw["client_dir"],
        server_host=raw.get("server_host", "127.0.0.1"),
        soap_port=int(raw.get("soap_port", 7878)),
        soap_user=raw["soap_user"],
        soap_password=raw["soap_password"],
        account=raw["account"],
        password=raw["password"],
        login_account_xy=tuple(raw["login_account_xy"]),
        login_password_xy=tuple(raw["login_password_xy"]),
        server_container=raw.get("server_container", ""),
        dashboard_url=raw.get("dashboard_url", ""),
        dashboard_user=raw.get("dashboard_user", ""),
        dashboard_password=raw.get("dashboard_password", ""),
        server_ssh=tuple(raw.get("server_ssh", ())),
        server_mmaps=raw.get("server_mmaps", "~/tortoise-wow-server/data/mmaps"),
        mmaps_cache=raw.get("mmaps_cache", ""),
    )
