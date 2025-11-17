import argparse
import logging
import random
import signal
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Generator, Tuple

import requests

# --- Configuration ---
API_KEY = "your_api_key"
API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

DEFAULT_SYMBOLS = ["ETH"]
DEFAULT_CONVERT = "USD"
DEFAULT_INTERVAL = 5  # seconds
TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2
MAX_BACKOFF = 30

HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": API_KEY,
}

# --- Optional colored output ---
try:
    from colorama import Fore, Style, init

    init(autoreset=True)
    COLOR_ENABLED = True
except ImportError:
    COLOR_ENABLED = False


def colorize(text: str, color: str) -> str:
    return f"{color}{text}{Style.RESET_ALL}" if COLOR_ENABLED else text


def setup_logger(debug: bool = False) -> logging.Logger:
    """Configure and return a logger."""
    logger = logging.getLogger("CryptoTracker")

    # Reset handlers to avoid duplicates if logger is recreated
    if logger.hasHandlers():
        logger.handlers.clear()

    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] - %(levelname)s - %(message)s"))
    logger.addHandler(handler)

    logging.getLogger("requests").setLevel(logging.WARNING)
    return logger


session = requests.Session()
session.headers.update(HEADERS)


def exponential_backoff(attempt: int) -> float:
    """Return an exponential backoff delay with jitter."""
    base = RETRY_BASE_DELAY ** attempt
    jitter = random.uniform(0.1, 0.9)
    return min(base + jitter, MAX_BACKOFF)


def fetch_crypto_price(symbol: str, convert: str, logger: logging.Logger) -> Optional[float]:
    """Fetch current price for a given symbol, retrying on transient errors."""
    params = {"symbol": symbol, "convert": convert}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            start = time.perf_counter()
            resp = session.get(API_URL, params=params, timeout=TIMEOUT)
            resp.raise_for_status()

            data = resp.json()
            price = data["data"][symbol]["quote"][convert]["price"]

            elapsed = time.perf_counter() - start
            logger.debug(f"[{symbol}] Response in {elapsed:.2f}s (attempt {attempt})")
            return price

        except (requests.Timeout, requests.ConnectionError) as e:
            delay = exponential_backoff(attempt)
            logger.warning(f"[{symbol}] Network error: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)

        except (KeyError, ValueError) as e:
            logger.error(f"[{symbol}] Invalid API response: {e}")
            return None

        except requests.RequestException as e:
            status = getattr(e.response, "status_code", "N/A")
            logger.error(f"[{symbol}] HTTP error {status}: {e}")
            return None

    logger.error(f"[{symbol}] Failed after {MAX_RETRIES} retries.")
    return None


def track_price(symbol: str, convert: str, interval: int, stop_event: threading.Event, logger: logging.Logger) -> None:
    """Continuously fetch and display price updates."""
    last_price: Optional[float] = None
    logger.info(f"[{symbol}] Tracking started.")

    while not stop_event.is_set():
        price = fetch_crypto_price(symbol, convert, logger)

        if price is not None:
            if last_price is None:
                diff_color = Fore.YELLOW
            elif price > last_price:
                diff_color = Fore.GREEN
            elif price < last_price:
                diff_color = Fore.RED
            else:
                diff_color = Fore.YELLOW

            price_str = f"${price:,.2f}"
            logger.info(f"[{symbol}] Price: {colorize(price_str, diff_color)} {convert}")
            last_price = price
        else:
            msg = f"[{symbol}] Price unavailable."
            if last_price is not None:
                msg += f" Last known: ${last_price:,.2f}"
            logger.warning(msg)

        stop_event.wait(interval)

    logger.info(f"[{symbol}] Tracking stopped.")


def wait_for_exit(stop_event: threading.Event, logger: logging.Logger) -> None:
    """Block until user presses Enter or CTRL+C."""
    try:
        if sys.stdin.isatty():
            input("Press Enter to exit...\n")
        else:
            stop_event.wait()
    except (KeyboardInterrupt, EOFError):
        logger.info("Exit requested by user.")
    finally:
        stop_event.set()


@contextmanager
def graceful_shutdown(executor: ThreadPoolExecutor, stop_event: threading.Event, logger: logging.Logger) -> Generator:
    """Ensure clean shutdown of all threads."""
    try:
        yield
    finally:
        logger.info("Shutting down...")
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=True)
        logger.info("All threads finished.")


def setup_signal_handlers(stop_event: threading.Event, logger: logging.Logger) -> None:
    """Handle SIGINT/SIGTERM for clean exit."""
    def handler(signum, _frame):
        logger.info(f"Signal {signum} received. Terminating...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def parse_args() -> Tuple[list, str, int, bool]:
    parser = argparse.ArgumentParser(description="Track live cryptocurrency prices via CoinMarketCap API.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to track (e.g., BTC ETH)")
    parser.add_argument("--convert", default=DEFAULT_CONVERT, help="Currency to convert to (default: USD)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Polling interval in seconds")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")

    args = parser.parse_args()

    if not API_KEY or API_KEY == "your_api_key":
        sys.exit("Error: Please set your CoinMarketCap API key in the script.")

    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    if not symbols:
        sys.exit("Error: No valid symbols provided.")

    return symbols, args.convert.upper(), max(1, args.interval), args.debug


def main():
    symbols, convert, interval, debug = parse_args()
    logger = setup_logger(debug)

    stop_event = threading.Event()
    setup_signal_handlers(stop_event, logger)

    with ThreadPoolExecutor(max_workers=len(symbols) + 1, thread_name_prefix="Tracker") as executor, \
            graceful_shutdown(executor, stop_event, logger):

        for symbol in symbols:
            executor.submit(track_price, symbol, convert, interval, stop_event, logger)

        executor.submit(wait_for_exit, stop_event, logger)
        stop_event.wait()


if __name__ == "__main__":
    main()
