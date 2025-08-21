import requests
import time
import threading
import logging
import sys
import signal
import argparse
import random
from typing import Optional, Generator
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

# --- Config ---
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


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("CryptoTracker")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


logger = setup_logger()


def colorize(text: str, color_code: str) -> str:
    return f"{color_code}{text}{Style.RESET_ALL}" if COLOR_ENABLED else text


session = requests.Session()
session.headers.update(HEADERS)


def fetch_crypto_price(symbol: str, convert: str) -> Optional[float]:
    params = {"symbol": symbol, "convert": convert}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"[{symbol}] Fetching price (attempt {attempt})...")
            response = session.get(API_URL, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            return response.json()["data"][symbol]["quote"][convert]["price"]
        except (requests.Timeout, requests.ConnectionError) as e:
            delay = min(RETRY_BASE_DELAY ** attempt + random.uniform(0, 1), MAX_BACKOFF)
            logger.warning(f"[{symbol}] Network error: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)
        except (KeyError, ValueError) as e:
            logger.error(f"[{symbol}] Invalid API response: {e}")
            break
        except requests.RequestException as e:
            logger.error(f"[{symbol}] HTTP error: {e}")
            break
    return None


def track_price(symbol: str, convert: str, interval: int, stop_event: threading.Event) -> None:
    last_price = None
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
            msg = f"Price unavailable. Last known: ${last_price:,.2f}" if last_price else "No price data available."
            logger.warning(f"[{symbol}] {msg}")
        stop_event.wait(interval)


def wait_for_exit(stop_event: threading.Event) -> None:
    try:
        input("Press Enter to exit...\n")
    except (KeyboardInterrupt, EOFError):
        logger.info("Interrupted by user.")
    finally:
        stop_event.set()


@contextmanager
def graceful_shutdown(executor: ThreadPoolExecutor, stop_event: threading.Event) -> Generator:
    try:
        yield
    finally:
        logger.info("Shutting down...")
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=True)
        logger.info("All tasks completed. Exiting.")


def setup_signal_handlers(stop_event: threading.Event) -> None:
    def handle_signal(signum, frame):
        logger.info(f"Signal {signum} received. Stopping...")
        stop_event.set()
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def parse_args():
    parser = argparse.ArgumentParser(description="Track live cryptocurrency prices from CoinMarketCap.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to track (e.g., BTC ETH)")
    parser.add_argument("--convert", default=DEFAULT_CONVERT, help="Currency to convert to (e.g., USD)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Polling interval in seconds")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        requests_log = logging.getLogger("requests.packages.urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True

    return [s.upper() for s in args.symbols], args.convert.upper(), max(1, args.interval)


def main():
    symbols, convert, interval = parse_args()
    stop_event = threading.Event()
    setup_signal_handlers(stop_event)

    with ThreadPoolExecutor(max_workers=len(symbols) + 1) as executor, graceful_shutdown(executor, stop_event):
        for symbol in symbols:
            executor.submit(track_price, symbol, convert, interval, stop_event)
        executor.submit(wait_for_exit, stop_event)
        stop_event.wait()


if __name__ == "__main__":
    main()
