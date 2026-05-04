"""Multi-connection registry: persist and load N TWS endpoints.

Each connection has a stable `label` (the human-readable id used by the
UI and the API), plus host/port/client_id and the soft attributes
(auto-connect, paper/live hint). Stored in `data_cache/connections.json`
so multiple TWS instances (e.g. paper + live, or two trading machines
on a LAN) can be managed without juggling `IB_BROKER_*` env vars.

On first run the file is seeded from the legacy `IB_BROKER_HOST/PORT/
CLIENT_ID/ACCOUNT/PAPER` env vars so existing single-connection setups
keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scanner.broker.ib_broker import IBBrokerConfig

log = logging.getLogger(__name__)


@dataclass
class ConnectionConfig:
    """User-facing connection definition.

    Mirrors the NanoPulse schema (label/host/port/client_id) so the same
    `connections.json` could in principle be shared between apps.
    """

    label: str
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 28
    # Hint only; the actual paper/live status is detected from the IB
    # account ID prefix once the connection is up. UI reads this for the
    # initial badge before authentication completes.
    paper: bool = True
    # Connect on API-server startup. Defaults to True so a single-conn
    # setup keeps the same "always connected" feel as the env-var path.
    auto_connect: bool = True
    # Optional default account to route orders at when the caller didn't
    # specify a target. Must be one of the connection's managed accounts.
    default_account: str | None = None

    def to_broker_config(self, journal_path: Path | None) -> IBBrokerConfig:
        """Materialize into the broker-layer config the IBBroker takes."""
        return IBBrokerConfig(
            host=self.host,
            port=int(self.port),
            client_id=int(self.client_id),
            account=self.default_account,
            journal_path=journal_path,
            force_paper=self.paper,
        )


def _connections_path() -> Path:
    """Where the JSON config lives. Same root as the parquet bar cache so
    you only have one data dir to back up."""
    cache_root = Path(
        os.environ.get(
            "BULLISH_CACHE_DIR",
            str(Path(__file__).resolve().parent.parent.parent / "data_cache"),
        )
    )
    return cache_root / "connections.json"


def _seed_from_env() -> list[ConnectionConfig]:
    """Build a single 'default' connection from the legacy env vars.

    Used the first time the API starts after this multi-connection
    refactor lands, so users with an existing `.env` don't have to
    reconfigure anything to keep their single-connection setup.
    """
    paper = os.environ.get("IB_BROKER_PAPER", "").strip().lower() in (
        "1", "true", "yes",
    )
    return [
        ConnectionConfig(
            label="default",
            host=os.environ.get("IB_BROKER_HOST", "127.0.0.1"),
            port=int(os.environ.get("IB_BROKER_PORT", "7497")),
            client_id=int(os.environ.get("IB_BROKER_CLIENT_ID", "28")),
            paper=paper,
            auto_connect=True,
            default_account=os.environ.get("IB_BROKER_ACCOUNT", "").strip() or None,
        ),
    ]


def load_connections() -> list[ConnectionConfig]:
    """Read the connections file. Seed-and-save if missing.

    Invalid entries (missing label, duplicate labels, malformed ports)
    are dropped with a warning rather than failing startup, so a typo in
    the JSON file can't take the whole API offline.
    """
    path = _connections_path()
    if not path.exists():
        seeded = _seed_from_env()
        save_connections(seeded)
        log.info(
            "connections.json missing — seeded one 'default' connection from env vars",
        )
        return seeded

    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        log.warning("connections.json unreadable (%s) — falling back to env seed", exc)
        return _seed_from_env()

    items = raw.get("connections") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        log.warning("connections.json has no `connections` array — using env seed")
        return _seed_from_env()

    out: list[ConnectionConfig] = []
    seen_labels: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "")).strip()
        if not label or label in seen_labels:
            log.warning("connections.json: skipping entry with bad/duplicate label: %r", entry)
            continue
        seen_labels.add(label)
        try:
            out.append(
                ConnectionConfig(
                    label=label,
                    host=str(entry.get("host", "127.0.0.1")),
                    port=int(entry.get("port", 7497)),
                    client_id=int(entry.get("client_id", 28)),
                    paper=bool(entry.get("paper", True)),
                    auto_connect=bool(entry.get("auto_connect", True)),
                    default_account=(
                        str(entry["default_account"]).strip()
                        if entry.get("default_account")
                        else None
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            log.warning("connections.json: skipping malformed entry %r: %s", entry, exc)

    if not out:
        log.warning("connections.json had no usable entries — using env seed")
        return _seed_from_env()
    return out


def save_connections(items: list[ConnectionConfig]) -> None:
    path = _connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"connections": [asdict(c) for c in items]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


# ----------------------------------------------------------------------
# Account aliases — friendly per-account names. Stored separately from
# the connection list so renaming "DUN394317 → mgn-ly-04" survives
# add/remove of connections, and so the accounts panel has a stable home
# whether the broker is currently connected or not.
# ----------------------------------------------------------------------


def _aliases_path() -> Path:
    cache_root = Path(
        os.environ.get(
            "BULLISH_CACHE_DIR",
            str(Path(__file__).resolve().parent.parent.parent / "data_cache"),
        )
    )
    return cache_root / "account_aliases.json"


def load_aliases() -> dict[str, str]:
    """Map of `account_id → alias`. Empty dict if the file is missing."""
    path = _aliases_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        log.warning("account_aliases.json unreadable (%s) — using empty map", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v}


def save_aliases(aliases: dict[str, str]) -> None:
    path = _aliases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(aliases, indent=2, sort_keys=True))
    tmp.replace(path)
