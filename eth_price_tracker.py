import argparse
import logging
import random
import signal
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Generator, Tuple, List, Dict

import requests

# ================================================================
# Configuration
# ================================================================
API_KEY = "your_api_key"
API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

DEFAULT_SYMBOLS = ["ETH"]
DEFAULT_CONVERT = "USD"
DEFAULT_INTERVAL = 5  # seconds

TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.8
MAX_BACKOFF = 30

# ================================================================
# Sessions & Headers
# ================================================================
def create_session(api_key: str) -> requests.Session:
    """Create a shared Session with preset headers."""
    session = requests.Session()
    session.headers.update({
        "Accepts": "application/json",
        "X-CMC_PRO_API_KEY": api_key,
    })
    return session

# ================================================================
# Optional Colored Output
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
# Logging
# ================================================================
def setup_logger(debug: bool) -> logging.Logger:
    """Create and configure the logger."""
    logger = logging.getLogger("CryptoTracker")

    if logger.hasHandlers():
        logger.handlers.clear()

    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(threadName)s] - %(levelname)s - %(message)s"
    ))
    logger.addHandler(handler)

    logging.getLogger("requests").setLevel(logging.WARNING)
    return logger

# ================================================================
# Backoff
# ================================================================
def exponential_backoff(attempt: int) -> float:
    """Exponential backoff with jitter."""
    delay = (RETRY_BASE_DELAY ** attempt) + random.uniform(0.1, 0.9)
    return min(delay, MAX_BACKOFF)

# ================================================================
# API Caller
# ================================================================
def fetch_price(
    session: requests.Session,
    symbol: str,
    convert: str,
    logger: logging.Logger
) -> Optional[float]:

    params = {"symbol": symbol, "convert": convert}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            t0 = time.perf_counter()
            resp = session.get(API_URL, params=params, timeout=TIMEOUT)
            resp.raise_for_status()

            data = resp.json()
            price = data["data"][symbol]["quote"][convert]["price"]

            logger.debug(f"[{symbol}] Response OK in {time.perf_counter() - t0:.2f}s (attempt {attempt})")
            return price

        except (requests.Timeout, requests.ConnectionError) as e:
            delay = exponential_backoff(attempt)
            logger.warning(f"[{symbol}] Network issue: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)

        except (ValueError, KeyError) as e:
            logger.error(f"[{symbol}] Bad API response: {e}")
            return None

        except requests.RequestException as e:
            logger.error(
                f"[{symbol}] HTTP error {getattr(e.response, 'status_code', 'N/A')}: {e}"
            )
            return None

    logger.error(f"[{symbol}] Max retries reached.")
    return None

# ================================================================
# Worker Thread
# ================================================================
def track_price(
    session: requests.Session,
    symbol: str,
    convert: str,
    interval: int,
    stop_event: threading.Event,
    logger: logging.Logger
) -> None:

    logger.info(f"[{symbol}] Tracking started.")
    last_price: Optional[float] = None

    while not stop_event.is_set():
        price = fetch_price(session, symbol, convert, logger)

        if price is not None:
            if last_price is None:
                diff_color = Fore.YELLOW
            elif price > last_price:
                diff_color = Fore.GREEN
            elif price < last_price:
                diff_color = Fore.RED
            else:
                diff_color = Fore.YELLOW

            msg = colorize(f"${price:,.2f}", diff_color)
            logger.info(f"[{symbol}] Price: {msg} {convert}")

            last_price = price

        else:
            text = f"[{symbol}] Price unavailable."
            if last_price is not None:
                text += f" Last known: ${last_price:,.2f}"
            logger.warning(text)

        stop_event.wait(interval)

    logger.info(f"[{symbol}] Tracking stopped.")

# ================================================================
# Input / Exit Handler
# ================================================================
def wait_for_exit(stop_event: threading.Event, logger: logging.Logger) -> None:
    try:
        if sys.stdin.isatty():
            input("Press Enter to exit...\n")
        else:
            stop_event.wait()
    except (KeyboardInterrupt, EOFError):
        logger.info("Exit requested.")
    finally:
        stop_event.set()

# ================================================================
# Graceful Shutdown
# ================================================================
@contextmanager
def graceful_shutdown(
    executor: ThreadPoolExecutor,
    stop_event: threading.Event,
    logger: logging.Logger
) -> Generator:
    try:
        yield
    finally:
        logger.info("Stopping...")
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=True)
        logger.info("All threads stopped.")

# ================================================================
# Signals
# ================================================================
def setup_signal_handlers(stop_event: threading.Event, logger: logging.Logger) -> None:
    def handler(signum, frame):
        logger.info(f"Received signal {signum}. Exiting...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)

# ================================================================
# CLI
# ================================================================
def parse_args() -> Tuple[List[str], str, int, bool]:
    parser = argparse.ArgumentParser(description="Track crypto prices via CoinMarketCap API.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--convert", default=DEFAULT_CONVERT)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    if not API_KEY or API_KEY == "your_api_key":
        sys.exit("Error: Please insert your CoinMarketCap API key.")

    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    if not symbols:
        sys.exit("Error: No valid symbols provided.")

    return symbols, args.convert.upper(), max(1, args.interval), args.debug

# ================================================================
# Main
# ================================================================
def main() -> None:
    symbols, convert, interval, debug = parse_args()
    logger = setup_logger(debug)

    session = create_session(API_KEY)
    stop_event = threading.Event()

    setup_signal_handlers(stop_event, logger)

    with ThreadPoolExecutor(
        max_workers=len(symbols) + 1,
        thread_name_prefix="Tracker"
    ) as executor, graceful_shutdown(executor, stop_event, logger):

        for s in symbols:
            executor.submit(track_price, session, s, convert, interval, stop_event, logger)

        executor.submit(wait_for_exit, stop_event, logger)

        stop_event.wait()


if __name__ == "__main__":
    main()
