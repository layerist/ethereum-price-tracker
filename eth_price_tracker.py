#!/usr/bin/env python3
"""
Concurrent crypto price tracker using CoinMarketCap API.

Production-grade features:
- Strict API response validation
- Thread-local HTTP sessions
- Deterministic retry strategy (429 + 5xx)
- Tuned connection pooling
- Explicit error boundaries
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
from concurrent.futures import ThreadPoolExecutor, Future
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
DEFAULT_TIMEOUT = 10


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
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = 5
    backoff_factor: float = 1.5
    pool_connections: int = 10
    pool_maxsize: int = 10


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
        connect=cfg.max_retries,
        read=cfg.max_retries,
        backoff_factor=cfg.backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
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


# ================================================================
# API Validation
# ================================================================
def validate_api_response(payload: Dict[str, Any]) -> None:
    status = payload.get("status")
    if not isinstance(status, dict):
        raise ValueError("Missing API status object")

    error_code = status.get("error_code")
    if error_code != 0:
        error_msg = status.get("error_message", "Unknown API error")
        raise RuntimeError(f"API error {error_code}: {error_msg}")


def parse_price(payload: Dict[str, Any], symbol: str, convert: str) -> float:
    validate_api_response(payload)

    try:
        return float(
            payload["data"][symbol]["quote"][convert]["price"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Malformed data for {symbol}/{convert}") from exc


# ================================================================
# Fetch Logic
# ================================================================
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

        payload: Dict[str, Any] = response.json()
        price = parse_price(payload, symbol, cfg.convert)

        logger.debug(
            "[%s] fetched in %.3fs",
            symbol,
            time.perf_counter() - start,
        )
        return price

    except requests.RequestException as exc:
        logger.warning("[%s] Network error: %s", symbol, exc)
    except RuntimeError as exc:
        logger.error("[%s] API error: %s", symbol, exc)
    except ValueError as exc:
        logger.error("[%s] Parse error: %s", symbol, exc)
    except Exception:
        logger.exception("[%s] Unexpected failure", symbol)

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
                    colorize(f"{price:,.2f}", color),
                    cfg.convert,
                )
                last_price = price
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
    stop_event: threading.Event,
    logger: logging.Logger,
) -> None:
    def handler(signum, _frame):
        logger.info("Signal %s received. Shutting down...", signum)
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
        sys.exit("Error: CMC_API_KEY environment variable not set")

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

    futures: List[Future] = []

    with ThreadPoolExecutor(
        max_workers=len(cfg.symbols),
        thread_name_prefix="Tracker",
    ) as executor:
        for symbol in cfg.symbols:
            futures.append(
                executor.submit(
                    track_price, symbol, cfg, stop_event, logger
                )
            )

        try:
            while not stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            stop_event.set()

        for future in futures:
            try:
                future.result()
            except Exception:
                logger.exception("Worker thread crashed")

    logger.info("Shutdown complete")


# ================================================================
if __name__ == "__main__":
    main()
