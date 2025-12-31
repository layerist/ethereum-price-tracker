#!/usr/bin/env python3
"""
Concurrent crypto price tracker using CoinMarketCap API.

Improvements:
- API key via environment variable
- Per-thread HTTP sessions (thread-safe)
- requests Retry adapter (HTTP 429 / 5xx aware)
- Dataclass-based configuration
- Clear separation of concerns
- Safer shutdown & signal handling
"""

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
from typing import Optional, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================================================================
# Optional colored output
# ================================================================
try:
    from colorama import Fore, Style, init

    init(autoreset=True)
    COLOR_ENABLED = True
except ImportError:
    COLOR_ENABLED = False

def colorize(text: str, color: str) -> str:
    return f"{color}{text}{Style.RESET_ALL}" if COLOR_ENABLED else text

# ================================================================
# Configuration
# ================================================================
API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

@dataclass(frozen=True)
class Config:
    symbols: List[str]
    convert: str
    interval: int
    debug: bool
    timeout: int = 10
    max_backoff: int = 30

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
    return logger

# ================================================================
# Session Factory (thread-safe)
# ================================================================
def create_session(api_key: str) -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=1.5,
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
            "X-CMC_PRO_API_KEY": api_key,
        }
    )
    return session

# ================================================================
# API Call
# ================================================================
def fetch_price(
    session: requests.Session,
    symbol: str,
    convert: str,
    timeout: int,
    logger: logging.Logger,
) -> Optional[float]:
    try:
        t0 = time.perf_counter()
        resp = session.get(
            API_URL,
            params={"symbol": symbol, "convert": convert},
            timeout=timeout,
        )
        resp.raise_for_status()

        data = resp.json()
        price = data["data"][symbol]["quote"][convert]["price"]

        logger.debug(
            f"[{symbol}] OK in {time.perf_counter() - t0:.2f}s"
        )
        return price

    except (KeyError, ValueError) as e:
        logger.error(f"[{symbol}] Invalid API response: {e}")
    except requests.RequestException as e:
        logger.warning(f"[{symbol}] Request failed: {e}")

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
    session = create_session(os.environ["CMC_API_KEY"])
    last_price: Optional[float] = None

    logger.info(f"[{symbol}] Tracking started")

    while not stop_event.is_set():
        price = fetch_price(
            session, symbol, cfg.convert, cfg.timeout, logger
        )

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
                f"[{symbol}] Price: "
                f"{colorize(f'${price:,.2f}', color)} {cfg.convert}"
            )
            last_price = price
        else:
            msg = f"[{symbol}] Price unavailable"
            if last_price is not None:
                msg += f" (last ${last_price:,.2f})"
            logger.warning(msg)

        stop_event.wait(cfg.interval)

    logger.info(f"[{symbol}] Tracking stopped")

# ================================================================
# Signals
# ================================================================
def setup_signal_handlers(stop_event: threading.Event, logger: logging.Logger) -> None:
    def handler(signum, frame):
        logger.info(f"Signal {signum} received, shutting down...")
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
        sys.exit("Error: set CMC_API_KEY environment variable")

    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    if not symbols:
        sys.exit("Error: no valid symbols provided")

    return Config(
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
        f"Starting tracker for {', '.join(cfg.symbols)} "
        f"(interval={cfg.interval}s)"
    )

    with ThreadPoolExecutor(
        max_workers=len(cfg.symbols),
        thread_name_prefix="Tracker",
    ) as executor:
        for symbol in cfg.symbols:
            executor.submit(
                track_price, symbol, cfg, stop_event, logger
            )

        try:
            while not stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            stop_event.set()

    logger.info("Shutdown complete")

# ================================================================
if __name__ == "__main__":
    main()
