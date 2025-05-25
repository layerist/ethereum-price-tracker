import requests
import time
import threading
import logging
import sys
import signal
from contextlib import contextmanager
from typing import Optional, List, Generator
import argparse

# Configuration Constants
API_KEY = "your_api_key"  # Replace with your CoinMarketCap API key
API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_SYMBOL = "ETH"
DEFAULT_CONVERT = "USD"
DEFAULT_INTERVAL = 5  # seconds
TIMEOUT = 10  # seconds
RETRY_DELAY = 3  # seconds
MAX_RETRIES = 3  # max retries for requests

HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": API_KEY,
}

# Logging configuration
logger = logging.getLogger("CryptoTracker")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)


def fetch_crypto_price(symbol: str = DEFAULT_SYMBOL, convert: str = DEFAULT_CONVERT) -> Optional[float]:
    """
    Fetch the latest cryptocurrency price from CoinMarketCap API with retry logic.
    """
    params = {"symbol": symbol, "convert": convert}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"Attempt {attempt}: Fetching {symbol} price...")
            response = requests.get(API_URL, headers=HEADERS, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            price = data["data"][symbol]["quote"][convert]["price"]
            return price
        except requests.Timeout:
            logger.warning(f"Timeout on attempt {attempt}. Retrying in {RETRY_DELAY}s...")
        except requests.ConnectionError:
            logger.warning(f"Connection error on attempt {attempt}. Retrying in {RETRY_DELAY}s...")
        except requests.RequestException as e:
            logger.error(f"Request failed: {e}")
            break
        except (KeyError, TypeError) as e:
            logger.error(f"Unexpected response format: {e}")
            break
        time.sleep(RETRY_DELAY)
    return None


def track_crypto_price(symbol: str, interval: int, stop_event: threading.Event) -> None:
    """
    Periodically fetch and log the cryptocurrency price.
    """
    last_price: Optional[float] = None
    while not stop_event.is_set():
        price = fetch_crypto_price(symbol)
        if price is not None:
            logger.info(f"{symbol} price: ${price:.2f} {DEFAULT_CONVERT}")
            last_price = price
        else:
            msg = f"Price unavailable. Last known: ${last_price:.2f}" if last_price else "No price data available."
            logger.warning(msg)
        stop_event.wait(interval)


@contextmanager
def graceful_shutdown(threads: List[threading.Thread], stop_event: threading.Event) -> Generator[None, None, None]:
    """
    Context manager to handle graceful shutdown of threads.
    """
    try:
        yield
    finally:
        logger.info("Stopping threads...")
        stop_event.set()
        for thread in threads:
            thread.join()
        logger.info("All threads stopped.")


def wait_for_exit(stop_event: threading.Event) -> None:
    """
    Wait for user to press Enter or Ctrl+C to stop the script.
    """
    try:
        input("Press Enter to stop...\n")
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt detected.")
    finally:
        stop_event.set()


def parse_arguments() -> tuple[str, int]:
    """
    Parse command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Track cryptocurrency prices in real-time.")
    parser.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL, help="Cryptocurrency symbol (e.g., ETH, BTC)")
    parser.add_argument("interval", nargs="?", type=int, default=DEFAULT_INTERVAL, help="Update interval in seconds")
    args = parser.parse_args()
    return args.symbol.upper(), max(1, args.interval)


def setup_signal_handlers(stop_event: threading.Event) -> None:
    """
    Setup signal handlers for graceful termination.
    """
    def handle_signal(signum, frame):
        logger.info(f"Signal {signum} received. Exiting...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def main() -> None:
    symbol, interval = parse_arguments()
    stop_event = threading.Event()

    setup_signal_handlers(stop_event)

    threads = [
        threading.Thread(target=track_crypto_price, args=(symbol, interval, stop_event), name="PriceTracker", daemon=True),
        threading.Thread(target=wait_for_exit, args=(stop_event,), name="InputThread", daemon=True)
    ]

    with graceful_shutdown(threads, stop_event):
        for thread in threads:
            thread.start()
        stop_event.wait()


if __name__ == "__main__":
    main()
