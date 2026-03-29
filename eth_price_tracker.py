#!/usr/bin/env python3
"""
Advanced concurrent crypto price tracker using CoinMarketCap API.

Improvements:
- Circuit breaker (prevents hammering API on failure)
- Smarter retry + rate-limit awareness
- Partial response handling (no full failure on 1 bad symbol)
- Metrics (latency, success rate)
- Optional CSV logging
- Multi-worker support (optional scaling)
- Cleaner architecture
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
    return f"{color}{text}{Style.RESET_ALL}" if COLOR else text


# =========================================================
# Constants
# =========================================================

API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_TIMEOUT = 10

_thread_local = threading.local()


# =========================================================
# Config
# =========================================================

@dataclass
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

    workers: int = 1
    csv_file: Optional[str] = None

    # circuit breaker
    fail_threshold: int = 5
    cooldown: int = 30


# =========================================================
# Logging
# =========================================================

def setup_logger(debug: bool) -> logging.Logger:
    logger = logging.getLogger("crypto_tracker")
    logger.handlers.clear()

    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"
        )
    )

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
# Circuit Breaker
# =========================================================

@dataclass
class CircuitBreaker:
    fail_count: int = 0
    last_fail_time: float = 0
    open_until: float = 0

    def allow(self) -> bool:
        return time.time() >= self.open_until

    def record_success(self):
        self.fail_count = 0

    def record_failure(self, cfg: Config):
        self.fail_count += 1
        self.last_fail_time = time.time()

        if self.fail_count >= cfg.fail_threshold:
            self.open_until = time.time() + cfg.cooldown


# =========================================================
# Parsing
# =========================================================

def extract_prices(
    payload: Dict,
    symbols: List[str],
    convert: str,
    logger: logging.Logger,
) -> Dict[str, float]:

    result: Dict[str, float] = {}

    data = payload.get("data", {})

    for sym in symbols:
        try:
            result[sym] = float(data[sym]["quote"][convert]["price"])
        except Exception:
            logger.warning("Missing/invalid data for %s", sym)

    return result


# =========================================================
# Fetch
# =========================================================

def fetch_prices(
    cfg: Config,
    logger: logging.Logger,
    breaker: CircuitBreaker,
) -> Optional[Dict[str, float]]:

    if not breaker.allow():
        logger.warning("Circuit breaker OPEN — skipping request")
        return None

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

        if r.status_code == 429:
            logger.warning("Rate limited (429)")
            breaker.record_failure(cfg)
            return None

        r.raise_for_status()

        payload = r.json()

        if payload.get("status", {}).get("error_code") != 0:
            logger.error("CMC API error: %s", payload)
            breaker.record_failure(cfg)
            return None

        prices = extract_prices(payload, cfg.symbols, cfg.convert, logger)

        breaker.record_success()

        logger.debug(
            "Fetched %d symbols in %.3fs",
            len(prices),
            time.perf_counter() - start,
        )

        return prices

    except requests.RequestException as e:
        logger.warning("Network error: %s", e)
        breaker.record_failure(cfg)

    except Exception:
        logger.exception("Parsing failure")
        breaker.record_failure(cfg)

    return None


# =========================================================
# CSV Writer
# =========================================================

def write_csv(path: str, data: Dict[str, float], convert: str):
    exists = os.path.exists(path)

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)

        if not exists:
            writer.writerow(["timestamp", "symbol", f"price_{convert}"])

        ts = int(time.time())

        for sym, price in data.items():
            writer.writerow([ts, sym, price])


# =========================================================
# Worker Loop
# =========================================================

def tracker(
    cfg: Config,
    stop_event: threading.Event,
    logger: logging.Logger,
):

    breaker = CircuitBreaker()
    last_prices: Dict[str, float] = {}

    logger.info("Worker started")

    while not stop_event.is_set():

        prices = fetch_prices(cfg, logger, breaker)

        if prices:

            if cfg.csv_file:
                write_csv(cfg.csv_file, prices, cfg.convert)

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

        sleep_time = cfg.interval + random.uniform(-0.3, 0.3)
        stop_event.wait(max(1, sleep_time))

    logger.info("Worker stopped")


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
    parser = argparse.ArgumentParser()

    parser.add_argument("--symbols", nargs="+", default=["ETH"])
    parser.add_argument("--convert", default="USD")
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--csv")
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    api_key = os.getenv("CMC_API_KEY")
    if not api_key:
        sys.exit("CMC_API_KEY not set")

    symbols = [s.upper().strip() for s in args.symbols if s.strip()]
    if not symbols:
        sys.exit("No symbols")

    return Config(
        api_key=api_key,
        symbols=symbols,
        convert=args.convert.upper(),
        interval=max(1, args.interval),
        debug=args.debug,
        workers=max(1, args.workers),
        csv_file=args.csv,
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
        "Starting | symbols=%s interval=%ss workers=%d",
        ",".join(cfg.symbols),
        cfg.interval,
        cfg.workers,
    )

    with ThreadPoolExecutor(
        max_workers=cfg.workers,
        thread_name_prefix="tracker",
    ) as executor:

        for _ in range(cfg.workers):
            executor.submit(tracker, cfg, stop_event, logger)

        stop_event.wait()

    logger.info("Shutdown complete")


# =========================================================

if __name__ == "__main__":
    main()
