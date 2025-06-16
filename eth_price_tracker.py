import requests
import time
import threading
import logging
import sys
import signal
import argparse
from typing import Optional, List, Generator, Tuple
from contextlib import contextmanager

# Optional: Pretty console logs
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
    COLOR_ENABLED = True
except ImportError:
    COLOR_ENABLED = False

# Constants
API_KEY = "your_api_key"  # Replace with your actual API key
API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_SYMBOL = "ETH"
DEFAULT_CONVERT = "USD"
DEFAULT_INTERVAL = 5  # in seconds
TIMEOUT = 10
RETRY_DELAY = 3
MAX_RETRIES = 3

HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": API_KEY,
}

# Logger setup
logger = logging.getLogger("CryptoTracker")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


def color(text: str, color_code: str) -> str:
    if not COLOR_ENABLED:
        return text
    return f"{color_code}{text}{Style.RESET_ALL}"


def fetch_crypto_price(symbol: str, convert: str = DEFAULT_CONVERT) -> Optional[float]:
    """
    Fetch the latest cryptocurrency price from CoinMarketCap.
    Retries on timeout/connection errors.
    """
    params = {"symbol": symbol, "convert": convert}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"Attempt {attempt}: fetching price for {symbol}...")
            response = requests.get(API_URL, headers=HEADERS, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return data["data"][symbol]["quote"][convert]["price"]
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(f"Network error on attempt {attempt}: {e}. Retrying in {RETRY_DELAY}s...")
        except requests.RequestException as e:
            logger.error(f"HTTP error: {e}")
            break
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Invalid response format: {e}")
            break
        time.sleep(RETRY_DELAY)
    return None


def track_crypto_price(symbol: str, interval: int, stop_event: threading.Event) -> None:
    """
    Continuously fetch and log crypto price at regular intervals.
    """
    last_price: Optional[float] = None
    while not stop_event.is_set():
        price = fetch_crypto_price(symbol)
        if price is not None:
            price_str = f"${price:.2f}"
            msg = f"{symbol} price: {color(price_str, Fore.GREEN)} {DEFAULT_CONVERT}"
            logger.info(msg)
            last_price = price
        else:
            if last_price is not None:
                logger.warning(f"Price unavailable. Last known: ${last_price:.2f}")
            else:
                logger.warning("No price data available.")
        stop_event.wait(interval)


@contextmanager
def graceful_shutdown(threads: List[threading.Thread], stop_event: threading.Event) -> Generator[None, None, None]:
    """
    Context manager to ensure all threads are cleanly terminated.
    """
    try:
        yield
    finally:
        logger.info("Stopping all threads...")
        stop_event.set()
        for t in threads:
            t.join()
        logger.info("Shutdown complete.")


def wait_for_exit(stop_event: threading.Event) -> None:
    """
    Waits for user to press Enter or Ctrl+C to exit.
    """
    try:
        input("Press Enter to exit...\n")
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        stop_event.set()


def parse_arguments() -> Tuple[str, int]:
    """
    Parse command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Track live cryptocurrency prices from CoinMarketCap.")
    parser.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL, help="Symbol to track (e.g. BTC, ETH)")
    parser.add_argument("interval", nargs="?", type=int, default=DEFAULT_INTERVAL, help="Polling interval (seconds)")
    args = parser.parse_args()
    return args.symbol.upper(), max(1, args.interval)


def setup_signal_handlers(stop_event: threading.Event) -> None:
    """
    Attach handlers to OS signals for graceful termination.
    """
    def signal_handler(signum, frame):
        logger.info(f"Signal {signum} received. Exiting...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def main() -> None:
    symbol, interval = parse_arguments()
    stop_event = threading.Event()
    setup_signal_handlers(stop_event)

    threads = [
        threading.Thread(target=track_crypto_price, args=(symbol, interval, stop_event), name="PriceTracker", daemon=True),
        threading.Thread(target=wait_for_exit, args=(stop_event,), name="InputListener", daemon=True),
    ]

    with graceful_shutdown(threads, stop_event):
        for thread in threads:
            thread.start()
        stop_event.wait()


if __name__ == "__main__":
    main()
