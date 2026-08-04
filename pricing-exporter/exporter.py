"""AI model pricing source and Prometheus exporter."""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# litellm setup (telemetry must be disabled before importing model_cost)
# ---------------------------------------------------------------------------


def _load_model_cost() -> dict[str, Any]:
    import litellm

    litellm.telemetry = False
    return litellm.model_cost


model_cost = _load_model_cost()

# LiteLLM's pinned local map can lag newly released models. Keep production
# offline and fill only missing official list-price fields.
OFFICIAL_PRICE_FALLBACKS: dict[str, dict[str, Any]] = {
    "gpt-5.6-terra": {
        "litellm_provider": "openai",
        "input_cost_per_token": 2.5e-6,
        "cache_read_input_token_cost": 0.25e-6,
        "output_cost_per_token": 15e-6,
    },
    "gpt-5.6-sol": {
        "litellm_provider": "openai",
        "input_cost_per_token": 5e-6,
        "cache_read_input_token_cost": 0.5e-6,
        "output_cost_per_token": 30e-6,
    },
}
official_price_models: set[str] = set()
for model_name, fallback in OFFICIAL_PRICE_FALLBACKS.items():
    info = model_cost.setdefault(model_name, {})
    missing = {key: value for key, value in fallback.items() if key not in info}
    if missing:
        info.update(missing)
        official_price_models.add(model_name)

# ---------------------------------------------------------------------------
# Config (all tunable via environment variables)
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "9101"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
EUR_API = "https://open.er-api.com/v6/latest/USD"
EUR_REFRESH = int(os.environ.get("EUR_REFRESH", "21600"))  # 6 h
STARTUP_RETRIES = int(os.environ.get("STARTUP_RETRIES", "5"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "30"))
DB_PATH = os.environ.get("DB_PATH", "/data/metrics.db")
PRICING_DIR = Path(os.environ.get("PRICING_DIR", "/pricing"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("pricing-exporter")

# Silence noisy third-party loggers
logging.getLogger("litellm").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Reuse TCP connections for recurring API calls
http = requests.Session()

# ---------------------------------------------------------------------------
# Mutable state
# ---------------------------------------------------------------------------
shutdown_event = threading.Event()
# Cached Prometheus text block, rebuilt only after sync_litellm_to_db().
_ai_metrics_cache: str = ""

# ---------------------------------------------------------------------------
# Database (all access must hold db_lock)
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_pricing (
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('input', 'cached', 'output')),
    price_per_million REAL NOT NULL,
    source          TEXT NOT NULL DEFAULT 'litellm',
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (provider, model, direction)
);
CREATE TABLE IF NOT EXISTS pricing_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    direction   TEXT NOT NULL,
    old_price   REAL,
    new_price   REAL NOT NULL,
    source      TEXT NOT NULL,
    changed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS exchange_rates (
    currency    TEXT PRIMARY KEY,
    rate        REAL NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def _init_db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)

    result = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if result and result[0] != "wal":
        log.warning("WAL mode not activated, got: %s", result[0])
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL)

    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
    elif row[0] != SCHEMA_VERSION:
        log.warning(
            "schema version mismatch (have %d, want %d)", row[0], SCHEMA_VERSION
        )
    conn.commit()

    log.info("db ready at %s", DB_PATH)
    return conn


db = _init_db()
db_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Provider / cost field mappings
# ---------------------------------------------------------------------------
PROVIDER_MAP: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "vertex_ai": "google",
    "vertex_ai_beta": "google",
    "vertex_ai-language-models": "google",
    "gemini": "google",
}
ALLOWED_PROVIDERS = frozenset({"anthropic", "openai", "google"})

COST_FIELDS: dict[str, str] = {
    "input": "input_cost_per_token",
    "cached": "cache_read_input_token_cost",
    "output": "output_cost_per_token",
}

# env var name -> (litellm model key, provider, direction)
OTEL_ENV_MAP: dict[str, tuple[str, str, str]] = {
    # OpenAI / Codex
    "GPT54_IN": ("gpt-5.4", "openai", "input"),
    "GPT54_CACHED": ("gpt-5.4", "openai", "cached"),
    "GPT54_OUT": ("gpt-5.4", "openai", "output"),
    "GPT54_MINI_IN": ("gpt-5.4-mini", "openai", "input"),
    "GPT54_MINI_CACHED": ("gpt-5.4-mini", "openai", "cached"),
    "GPT54_MINI_OUT": ("gpt-5.4-mini", "openai", "output"),
    "GPT5_MINI_IN": ("gpt-5-mini", "openai", "input"),
    "GPT5_MINI_CACHED": ("gpt-5-mini", "openai", "cached"),
    "GPT5_MINI_OUT": ("gpt-5-mini", "openai", "output"),
    "GPT55_IN": ("gpt-5.5", "openai", "input"),
    "GPT55_CACHED": ("gpt-5.5", "openai", "cached"),
    "GPT55_OUT": ("gpt-5.5", "openai", "output"),
    "GPT56_SOL_IN": ("gpt-5.6-sol", "openai", "input"),
    "GPT56_SOL_CACHED": ("gpt-5.6-sol", "openai", "cached"),
    "GPT56_SOL_OUT": ("gpt-5.6-sol", "openai", "output"),
    "GPT53_CODEX_IN": ("gpt-5.3-codex", "openai", "input"),
    "GPT53_CODEX_CACHED": ("gpt-5.3-codex", "openai", "cached"),
    "GPT53_CODEX_OUT": ("gpt-5.3-codex", "openai", "output"),
    # Gemini specific models
    "GEMINI_20_FLASH_IN": ("gemini-2.0-flash", "google", "input"),
    "GEMINI_20_FLASH_CACHED": ("gemini-2.0-flash", "google", "cached"),
    "GEMINI_20_FLASH_OUT": ("gemini-2.0-flash", "google", "output"),
    "GEMINI_25_FLASH_LITE_IN": ("gemini-2.5-flash-lite", "google", "input"),
    "GEMINI_25_FLASH_LITE_CACHED": ("gemini-2.5-flash-lite", "google", "cached"),
    "GEMINI_25_FLASH_LITE_OUT": ("gemini-2.5-flash-lite", "google", "output"),
    "GEMINI_25_FLASH_IN": ("gemini-2.5-flash", "google", "input"),
    "GEMINI_25_FLASH_CACHED": ("gemini-2.5-flash", "google", "cached"),
    "GEMINI_25_FLASH_OUT": ("gemini-2.5-flash", "google", "output"),
    "GEMINI_25_PRO_IN": ("gemini-2.5-pro", "google", "input"),
    "GEMINI_25_PRO_CACHED": ("gemini-2.5-pro", "google", "cached"),
    "GEMINI_25_PRO_OUT": ("gemini-2.5-pro", "google", "output"),
    "GEMINI_3X_FLASH_LITE_IN": ("gemini-3.1-flash-lite-preview", "google", "input"),
    "GEMINI_3X_FLASH_LITE_CACHED": (
        "gemini-3.1-flash-lite-preview",
        "google",
        "cached",
    ),
    "GEMINI_3X_FLASH_LITE_OUT": ("gemini-3.1-flash-lite-preview", "google", "output"),
    # Family fallbacks: intentionally duplicate current best-guess pricing.
    # These feed the OTel catch-all rules for future unknown model versions.
    "GEMINI_PRO_IN": ("gemini-2.5-pro", "google", "input"),
    "GEMINI_PRO_CACHED": ("gemini-2.5-pro", "google", "cached"),
    "GEMINI_PRO_OUT": ("gemini-2.5-pro", "google", "output"),
    "GEMINI_FLASH_IN": ("gemini-2.5-flash", "google", "input"),
    "GEMINI_FLASH_CACHED": ("gemini-2.5-flash", "google", "cached"),
    "GEMINI_FLASH_OUT": ("gemini-2.5-flash", "google", "output"),
}

# ---------------------------------------------------------------------------
# Pricing: litellm -> SQLite -> Prometheus + env file
# ---------------------------------------------------------------------------


def sync_litellm_to_db() -> None:
    """Upsert litellm pricing into SQLite, recording changes in history."""
    global _ai_metrics_cache
    inserted = updated = 0

    with db_lock:
        for model_name, info in model_cost.items():
            provider = PROVIDER_MAP.get(info.get("litellm_provider", ""), "")
            if provider not in ALLOWED_PROVIDERS:
                continue
            source = (
                "openai-official" if model_name in official_price_models else "litellm"
            )

            for direction, field in COST_FIELDS.items():
                cost = info.get(field)
                if cost is None:
                    continue
                per_m = cost * 1_000_000

                row = db.execute(
                    "SELECT price_per_million FROM model_pricing"
                    " WHERE provider=? AND model=? AND direction=?",
                    (provider, model_name, direction),
                ).fetchone()

                if row is None:
                    db.execute(
                        "INSERT INTO model_pricing"
                        " (provider, model, direction, price_per_million, source)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (provider, model_name, direction, per_m, source),
                    )
                    db.execute(
                        "INSERT INTO pricing_history"
                        " (provider, model, direction, old_price, new_price, source)"
                        " VALUES (?, ?, ?, NULL, ?, ?)",
                        (provider, model_name, direction, per_m, source),
                    )
                    inserted += 1
                elif abs(row[0] - per_m) > 0.0001:
                    db.execute(
                        "INSERT INTO pricing_history"
                        " (provider, model, direction, old_price, new_price, source)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (provider, model_name, direction, row[0], per_m, source),
                    )
                    db.execute(
                        "UPDATE model_pricing"
                        " SET price_per_million=?, source=?,"
                        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                        " WHERE provider=? AND model=? AND direction=?",
                        (per_m, source, provider, model_name, direction),
                    )
                    updated += 1

        db.commit()

    # Rebuild cached metrics block after sync
    _ai_metrics_cache = _build_ai_metrics()
    log.info("synced LiteLLM pricing: %d inserted, %d updated", inserted, updated)


def _sanitize_label(value: str) -> str:
    """Escape backslash, newline, and double-quote for Prometheus labels."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _build_ai_metrics() -> str:
    """Build Prometheus metrics text from the SQLite pricing table."""
    lines = [
        "# HELP ai_price_per_million AI model pricing in USD per 1M tokens",
        "# TYPE ai_price_per_million gauge",
    ]
    with db_lock:
        rows = db.execute(
            "SELECT provider, model, direction, price_per_million"
            " FROM model_pricing"
            " ORDER BY provider, model, direction"
        ).fetchall()

    for provider, model, direction, price in rows:
        p = _sanitize_label(provider)
        m = _sanitize_label(model)
        lines.append(
            f'ai_price_per_million{{provider="{p}",model="{m}",'
            f'direction="{direction}"}} {price}'
        )
    return "\n".join(lines)


def write_pricing_files() -> None:
    """Write one price file per OTel Collector variable."""
    if not PRICING_DIR.is_dir():
        log.info("pricing dir %s not mounted, skipping price files", PRICING_DIR)
        return

    written = 0

    with db_lock:
        for env_var, (model_key, provider, direction) in OTEL_ENV_MAP.items():
            row = db.execute(
                "SELECT price_per_million FROM model_pricing"
                " WHERE model=? AND direction=? AND provider=?",
                (model_key, direction, provider),
            ).fetchone()
            if row is not None:
                file_path = PRICING_DIR / env_var
                file_tmp = file_path.with_suffix(".tmp")
                file_tmp.write_text(f"{row[0]:g}")
                os.replace(file_tmp, file_path)
                written += 1
            else:
                log.warning("%s (%s/%s) not in DB", env_var, model_key, direction)

    log.info("wrote %d price files to %s", written, PRICING_DIR)


# ---------------------------------------------------------------------------
# EUR exchange rate (persisted to DB)
# ---------------------------------------------------------------------------


def _get_eur_rate() -> float:
    with db_lock:
        row = db.execute(
            "SELECT rate FROM exchange_rates WHERE currency='EUR'"
        ).fetchone()
    return row[0] if row else 0.88


def fetch_eur() -> bool:
    """Fetch USD→EUR rate and persist to DB."""
    try:
        resp = http.get(EUR_API, timeout=10)
        resp.raise_for_status()
        rate = resp.json()["rates"]["EUR"]
        if not (0.1 < rate < 10):
            log.warning("EUR rate out of range: %.4f", rate)
            return False
        with db_lock:
            db.execute(
                "INSERT INTO exchange_rates (currency, rate, updated_at)"
                " VALUES ('EUR', ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
                " ON CONFLICT(currency) DO UPDATE"
                " SET rate=excluded.rate, updated_at=excluded.updated_at",
                (rate,),
            )
            db.commit()
        log.info("EUR rate=%.4f", rate)
        return True
    except Exception:
        log.warning("EUR fetch failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Prometheus /metrics endpoint
# ---------------------------------------------------------------------------


def _render_metrics() -> str:
    eur = _get_eur_rate()
    return (
        f"# HELP usd_eur_exchange_rate Current USD to EUR exchange rate\n"
        f"# TYPE usd_eur_exchange_rate gauge\n"
        f"usd_eur_exchange_rate {eur}\n"
        f"{_ai_metrics_cache}\n"
    )


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _render_metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _fmt, *_args):
        pass  # suppress per-request logging


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------


def _refresh_loop(name: str, fn, interval: int) -> None:
    while not shutdown_event.is_set():
        shutdown_event.wait(interval)
        if shutdown_event.is_set():
            break
        try:
            fn()
        except Exception:
            log.warning("%s refresh failed", name, exc_info=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("starting pricing-exporter (port=%d, log=%s)", PORT, LOG_LEVEL)

    sync_litellm_to_db()
    write_pricing_files()

    for attempt in range(1, STARTUP_RETRIES + 1):
        if fetch_eur():
            break
        log.info(
            "EUR attempt %d/%d, retrying in %ds", attempt, STARTUP_RETRIES, RETRY_DELAY
        )
        time.sleep(RETRY_DELAY)

    threading.Thread(
        target=_refresh_loop, args=("EUR", fetch_eur, EUR_REFRESH), daemon=True
    ).start()

    server = HTTPServer(("0.0.0.0", PORT), _MetricsHandler)
    log.info("serving metrics on :%d", PORT)

    def _shutdown(_sig, _frame):
        log.info("shutting down")
        shutdown_event.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
