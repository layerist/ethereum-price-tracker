#!/usr/bin/env python3
"""
Concurrent crypto price tracker using CoinMarketCap API.

Production features:
- Thread-local HTTP sessions
- Batch symbol requests (rate-limit friendly)
- Deterministic retries for 429 + 5xx
- Connection pooling
- Graceful shutdown
- Structured logging
- Optional colored output
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =========================================================
# Optional colors
# =========================================================

try:
    from colorama import Fore, Style, init as colorama_init

    colorama_init(autoreset=True)
    COLOR = True
except Exception:
    COLOR = False


def colorize(text: str, color: str) -> str:
    if not COLOR:
        return text
    return f"{color}{text}{Style.RESET_ALL}"


# =========================================================
# Constants
# =========================================================

API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_TIMEOUT = 10

_thread_local = threading.local()


# =========================================================
# Config
# =========================================================

@dataclass(frozen=True)
class Config:
    api_key: str
    symbols: List[str]
    convert: str
    interval: int
    debug: bool

    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = 5
    backoff_factor: float = 1.2

    pool_connections: int = 20
    pool_maxsize: int = 20


# =========================================================
# Logging
# =========================================================

def setup_logger(debug: bool) -> logging.Logger:
    logger = logging.getLogger("crypto_tracker")
    logger.handlers.clear()

    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    return logger


# =========================================================
# Session Factory
# =========================================================

def create_session(cfg: Config) -> requests.Session:

    retry = Retry(
        total=cfg.max_retries,
        read=cfg.max_retries,
        connect=cfg.max_retries,
        backoff_factor=cfg.backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=cfg.pool_connections,
        pool_maxsize=cfg.pool_maxsize,
    )

    session = requests.Session()
    session.mount("https://", adapter)

    session.headers.update(
        {
            "Accept": "application/json",
            "X-CMC_PRO_API_KEY": cfg.api_key,
        }
    )

    return session


def get_session(cfg: Config) -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = create_session(cfg)
    return _thread_local.session


# =========================================================
# API Parsing
# =========================================================

def validate_payload(payload: Dict) -> None:
    status = payload.get("status")

    if not isinstance(status, dict):
        raise RuntimeError("Missing API status")

    if status.get("error_code") != 0:
        raise RuntimeError(
            f"CMC error {status.get('error_code')}: {status.get('error_message')}"
        )


def extract_prices(payload: Dict, symbols: List[str], convert: str) -> Dict[str, float]:
    validate_payload(payload)

    result: Dict[str, float] = {}

    for sym in symbols:
        try:
            price = payload["data"][sym]["quote"][convert]["price"]
            result[sym] = float(price)
        except Exception:
            raise ValueError(f"Malformed price for {sym}")

    return result


# =========================================================
# Fetch
# =========================================================

def fetch_prices(cfg: Config, logger: logging.Logger) -> Optional[Dict[str, float]]:
    session = get_session(cfg)

    try:
        start = time.perf_counter()

        r = session.get(
            API_URL,
            params={
                "symbol": ",".join(cfg.symbols),
                "convert": cfg.convert,
            },
            timeout=cfg.timeout,
        )

        r.raise_for_status()

        payload = r.json()

        prices = extract_prices(payload, cfg.symbols, cfg.convert)

        logger.debug(
            "Fetched %d symbols in %.3fs",
            len(prices),
            time.perf_counter() - start,
        )

        return prices

    except requests.RequestException as e:
        logger.warning("Network error: %s", e)

    except Exception:
        logger.exception("Failed parsing API response")

    return None


# =========================================================
# Tracker Loop
# =========================================================

def tracker(cfg: Config, stop_event: threading.Event, logger: logging.Logger):

    last_prices: Dict[str, float] = {}

    logger.info("Tracking started")

    while not stop_event.is_set():

        prices = fetch_prices(cfg, logger)

        if prices:

            for symbol, price in prices.items():

                prev = last_prices.get(symbol)

                if prev is None:
                    color = Fore.YELLOW
                elif price > prev:
                    color = Fore.GREEN
                elif price < prev:
                    color = Fore.RED
                else:
                    color = Fore.YELLOW

                logger.info(
                    "%s: %s %s",
                    symbol,
                    colorize(f"{price:,.2f}", color),
                    cfg.convert,
                )

                last_prices[symbol] = price

        else:
            logger.warning("Price update failed")

        # jitter prevents synchronized bursts
        sleep_time = cfg.interval + random.uniform(-0.3, 0.3)
        stop_event.wait(max(1, sleep_time))

    logger.info("Tracker stopped")


# =========================================================
# Signals
# =========================================================

def setup_signals(stop_event: threading.Event, logger: logging.Logger):

    def handler(sig, frame):
        logger.info("Signal %s received — shutting down", sig)
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handler)
        except Exception:
            pass


# =========================================================
# CLI
# =========================================================

def parse_args() -> Config:

    parser = argparse.ArgumentParser(
        description="CoinMarketCap crypto price tracker"
    )

    parser.add_argument("--symbols", nargs="+", default=["ETH"])
    parser.add_argument("--convert", default="USD")
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    api_key = os.getenv("CMC_API_KEY")

    if not api_key:
        sys.exit("CMC_API_KEY env variable not set")

    symbols = [s.upper().strip() for s in args.symbols if s.strip()]

    if not symbols:
        sys.exit("No valid symbols")

    return Config(
        api_key=api_key,
        symbols=symbols,
        convert=args.convert.upper(),
        interval=max(1, args.interval),
        debug=args.debug,
    )


# =========================================================
# Main
# =========================================================

def main():

    cfg = parse_args()

    logger = setup_logger(cfg.debug)

    stop_event = threading.Event()

    setup_signals(stop_event, logger)

    logger.info(
        "Starting tracker | symbols=%s interval=%ss",
        ",".join(cfg.symbols),
        cfg.interval,
    )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tracker"):
        tracker(cfg, stop_event, logger)

    logger.info("Shutdown complete")


# =========================================================

if __name__ == "__main__":
    main()
