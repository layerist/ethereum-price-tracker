#!/usr/bin/env python3
"""
Concurrent crypto price tracker using CoinMarketCap API.

Features:
- API key via environment variable
- One HTTP session per thread (thread-safe)
- Automatic retries for rate limits and server errors
- Dataclass-based configuration
- Graceful shutdown via signals
- Optional colored output
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================================================================
# Optional colored output
# ================================================================
try:
    from colorama import Fore, Style, init as colorama_init

    colorama_init(autoreset=True)
    COLOR_ENABLED = True
except ImportError:
    COLOR_ENABLED = False


def colorize(text: str, color: str) -> str:
    if not COLOR_ENABLED:
        return text
    return f"{color}{text}{Style.RESET_ALL}"


# ================================================================
# Constants
# ================================================================
API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

# ================================================================
# Configuration
# ================================================================
@dataclass(frozen=True)
class Config:
    api_key: str
    symbols: List[str]
    convert: str
    interval: int
    debug: bool
    timeout: int = 10
    max_retries: int = 5
    backoff_factor: float = 1.5


# ================================================================
# Logging
# ================================================================
def setup_logger(debug: bool) -> logging.Logger:
    logger = logging.getLogger("CryptoTracker")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(threadName)s] %(levelname)s: %(message)s"
        )
    )
    logger.addHandler(handler)

    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logger


# ================================================================
# HTTP Session Factory
# ================================================================
def create_session(cfg: Config) -> requests.Session:
    retry = Retry(
        total=cfg.max_retries,
        backoff_factor=cfg.backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "Accepts": "application/json",
            "X-CMC_PRO_API_KEY": cfg.api_key,
        }
    )
    return session


# ================================================================
# API Logic
# ================================================================
def parse_price(
    payload: Dict[str, Any], symbol: str, convert: str
) -> float:
    return payload["data"][symbol]["quote"][convert]["price"]


def fetch_price(
    session: requests.Session,
    symbol: str,
    cfg: Config,
    logger: logging.Logger,
) -> Optional[float]:
    try:
        start = time.perf_counter()

        response = session.get(
            API_URL,
            params={"symbol": symbol, "convert": cfg.convert},
            timeout=cfg.timeout,
        )
        response.raise_for_status()

        data = response.json()
        price = parse_price(data, symbol, cfg.convert)

        logger.debug(
            "[%s] fetched in %.2fs",
            symbol,
            time.perf_counter() - start,
        )
        return float(price)

    except (KeyError, TypeError, ValueError) as exc:
        logger.error("[%s] Malformed API response: %s", symbol, exc)
    except requests.RequestException as exc:
        logger.warning("[%s] Request error: %s", symbol, exc)

    return None


# ================================================================
# Worker
# ================================================================
def track_price(
    symbol: str,
    cfg: Config,
    stop_event: threading.Event,
    logger: logging.Logger,
) -> None:
    session = create_session(cfg)
    last_price: Optional[float] = None

    logger.info("[%s] Tracking started", symbol)

    try:
        while not stop_event.is_set():
            price = fetch_price(session, symbol, cfg, logger)

            if price is not None:
                if last_price is None:
                    color = Fore.YELLOW
                elif price > last_price:
                    color = Fore.GREEN
                elif price < last_price:
                    color = Fore.RED
                else:
                    color = Fore.YELLOW

                logger.info(
                    "[%s] Price: %s %s",
                    symbol,
                    colorize(f"${price:,.2f}", color),
                    cfg.convert,
                )
                last_price = price
            else:
                if last_price is not None:
                    logger.warning(
                        "[%s] Price unavailable (last $%.2f)",
                        symbol,
                        last_price,
                    )
                else:
                    logger.warning("[%s] Price unavailable", symbol)

            stop_event.wait(cfg.interval)

    finally:
        session.close()
        logger.info("[%s] Tracking stopped", symbol)


# ================================================================
# Signal Handling
# ================================================================
def setup_signal_handlers(
    stop_event: threading.Event, logger: logging.Logger
) -> None:
    def handler(signum, _frame):
        logger.info("Signal %s received, shutting down...", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


# ================================================================
# CLI
# ================================================================
def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Track crypto prices via CoinMarketCap API"
    )
    parser.add_argument("--symbols", nargs="+", default=["ETH"])
    parser.add_argument("--convert", default="USD")
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    api_key = os.getenv("CMC_API_KEY")
    if not api_key:
        sys.exit("Error: CMC_API_KEY environment variable is not set")

    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    if not symbols:
        sys.exit("Error: no valid symbols provided")

    return Config(
        api_key=api_key,
        symbols=symbols,
        convert=args.convert.upper(),
        interval=max(1, args.interval),
        debug=args.debug,
    )


# ================================================================
# Main
# ================================================================
def main() -> None:
    cfg = parse_args()
    logger = setup_logger(cfg.debug)

    stop_event = threading.Event()
    setup_signal_handlers(stop_event, logger)

    logger.info(
        "Starting tracker for %s (interval=%ss)",
        ", ".join(cfg.symbols),
        cfg.interval,
    )

    with ThreadPoolExecutor(
        max_workers=len(cfg.symbols),
        thread_name_prefix="Tracker",
    ) as executor:
        for symbol in cfg.symbols:
            executor.submit(track_price, symbol, cfg, stop_event, logger)

        try:
            while not stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            stop_event.set()

    logger.info("Shutdown complete")


# ================================================================
if __name__ == "__main__":
    main()
