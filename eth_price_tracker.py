import argparse
import logging
import random
import signal
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Generator

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


def colorize(text: str, color_code: str) -> str:
    return f"{color_code}{text}{Style.RESET_ALL}" if COLOR_ENABLED else text


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("CryptoTracker")
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s [%(threadName)s] - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


logger = setup_logger()

session = requests.Session()
session.headers.update(HEADERS)


def exponential_backoff(attempt: int) -> float:
    """Return delay time with exponential backoff and jitter."""
    delay = min(RETRY_BASE_DELAY ** attempt + random.uniform(0, 1), MAX_BACKOFF)
    return delay


def fetch_crypto_price(symbol: str, convert: str) -> Optional[float]:
    """Fetch current price for a symbol with retry and exponential backoff."""
    params = {"symbol": symbol, "convert": convert}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            start_time = time.perf_counter()
            response = session.get(API_URL, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()

            price = data["data"][symbol]["quote"][convert]["price"]
            elapsed = time.perf_counter() - start_time
            logger.debug(f"[{symbol}] Fetched in {elapsed:.2f}s (attempt {attempt})")

            return price

        except (requests.Timeout, requests.ConnectionError) as e:
            delay = exponential_backoff(attempt)
            logger.warning(f"[{symbol}] Network error: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)

        except (KeyError, ValueError) as e:
            logger.error(f"[{symbol}] Invalid API response: {e}")
            break

        except requests.RequestException as e:
            status_code = getattr(e.response, "status_code", "N/A")
            logger.error(f"[{symbol}] HTTP error {status_code}: {e}")
            break

    return None


def track_price(symbol: str, convert: str, interval: int, stop_event: threading.Event) -> None:
    """Continuously fetch and log price updates for a symbol."""
    last_price = None
    thread_name = threading.current_thread().name
    logger.info(f"[{symbol}] Tracking started on {thread_name}")

    while not stop_event.is_set():
        price = fetch_crypto_price(symbol, convert)

        if price is not None:
            price_str = f"${price:,.2f}"
            color_code = Fore.YELLOW

            if last_price is not None:
                if price > last_price:
                    color_code = Fore.GREEN
                elif price < last_price:
                    color_code = Fore.RED

            logger.info(f"[{symbol}] Price: {colorize(price_str, color_code)} {convert}")
            last_price = price

        else:
            if last_price is not None:
                logger.warning(f"[{symbol}] Price unavailable. Last known: ${last_price:,.2f}")
            else:
                logger.warning(f"[{symbol}] No price data available.")

        stop_event.wait(interval)

    logger.info(f"[{symbol}] Tracking stopped.")


def wait_for_exit(stop_event: threading.Event) -> None:
    """Wait for user to press Enter or handle interruption."""
    try:
        if sys.stdin.isatty():
            input("Press Enter to exit...\n")
        else:
            stop_event.wait()
    except (KeyboardInterrupt, EOFError):
        logger.info("Interrupted by user.")
    finally:
        stop_event.set()


@contextmanager
def graceful_shutdown(executor: ThreadPoolExecutor, stop_event: threading.Event) -> Generator:
    """Ensure clean shutdown on exit."""
    try:
        yield
    finally:
        logger.info("Shutting down gracefully...")
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=True)
        logger.info("All tasks completed. Exiting.")


def setup_signal_handlers(stop_event: threading.Event) -> None:
    def handle_signal(signum, _frame):
        logger.info(f"Signal {signum} received. Stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def parse_args():
    parser = argparse.ArgumentParser(description="Track live cryptocurrency prices via CoinMarketCap API.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to track (e.g. BTC ETH)")
    parser.add_argument("--convert", default=DEFAULT_CONVERT, help="Currency to convert to (e.g. USD)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Polling interval (seconds)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("requests").setLevel(logging.WARNING)

    if not API_KEY or API_KEY == "your_api_key":
        logger.error("Missing CoinMarketCap API key. Please set API_KEY before running.")
        sys.exit(1)

    return [s.upper() for s in args.symbols], args.convert.upper(), max(1, args.interval)


def main():
    symbols, convert, interval = parse_args()
    stop_event = threading.Event()
    setup_signal_handlers(stop_event)

    with ThreadPoolExecutor(max_workers=len(symbols) + 1, thread_name_prefix="Tracker") as executor, \
            graceful_shutdown(executor, stop_event):

        for symbol in symbols:
            executor.submit(track_price, symbol, convert, interval, stop_event)

        executor.submit(wait_for_exit, stop_event)
        stop_event.wait()


if __name__ == "__main__":
    main()
