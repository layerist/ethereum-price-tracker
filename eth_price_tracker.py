import requests
import time
import threading
import logging
import sys
import signal
from contextlib import contextmanager
from typing import Optional, List, Generator
from typing_extensions import Final
import argparse

# Configuration Constants
API_KEY: Final[str] = "your_api_key"  # Replace with your CoinMarketCap API key
API_URL: Final[str] = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_SYMBOL: Final[str] = "ETH"
DEFAULT_CONVERT: Final[str] = "USD"
DEFAULT_INTERVAL: Final[int] = 5  # seconds
TIMEOUT: Final[int] = 10  # request timeout in seconds
RETRY_DELAY: Final[int] = 3  # seconds
MAX_RETRIES: Final[int] = 3  # request retries

HEADERS: Final[dict] = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": API_KEY,
}

# Logging configuration
logger = logging.getLogger("CryptoTracker")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)


def fetch_crypto_price(symbol: str = DEFAULT_SYMBOL, convert: str = DEFAULT_CONVERT) -> Optional[float]:
    """Fetch the latest cryptocurrency price from CoinMarketCap with retry logic."""
    params = {"symbol": symbol, "convert": convert}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"[Attempt {attempt}] Fetching price for {symbol}...")
            response = requests.get(API_URL, headers=HEADERS, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return data["data"][symbol]["quote"][convert]["price"]
        except (requests.Timeout, requests.ConnectionError):
            logger.warning("Network issue. Retrying...")
        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            break
        except (KeyError, TypeError) as e:
            logger.error(f"Invalid response format: {e}")
            break
        time.sleep(RETRY_DELAY)
    return None


def track_crypto_price(symbol: str, interval: int, stop_event: threading.Event) -> None:
    """Periodically fetch and log the cryptocurrency price."""
    last_price: Optional[float] = None
    while not stop_event.is_set():
        price = fetch_crypto_price(symbol)
        if price is not None:
            logger.info(f"{symbol} price: ${price:.2f} {DEFAULT_CONVERT}")
            last_price = price
        elif last_price is not None:
            logger.warning(f"Price unavailable. Last known: ${last_price:.2f} {DEFAULT_CONVERT}")
        else:
            logger.warning("No price data available.")
        stop_event.wait(interval)


@contextmanager
def graceful_shutdown(threads: List[threading.Thread], stop_event: threading.Event) -> Generator[None, None, None]:
    """Context manager to shut down threads on exit."""
    try:
        yield
    finally:
        logger.info("Stopping threads...")
        stop_event.set()
        for thread in threads:
            thread.join()
        logger.info("All threads stopped.")


def wait_for_exit(stop_event: threading.Event) -> None:
    """Wait for the user to press Enter or use Ctrl+C to exit."""
    input("Press Enter to stop the script...\n")
    stop_event.set()


def parse_arguments() -> tuple[str, int]:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Track cryptocurrency prices in real-time.")
    parser.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL, help="Cryptocurrency symbol (e.g., ETH, BTC)")
    parser.add_argument("interval", nargs="?", type=int, default=DEFAULT_INTERVAL, help="Update interval in seconds")
    args = parser.parse_args()
    return args.symbol.upper(), max(1, args.interval)


def main() -> None:
    symbol, interval = parse_arguments()
    stop_event = threading.Event()

    def handle_interrupt(signum, frame):
        logger.info("Interrupt received, stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)

    threads = [
        threading.Thread(target=wait_for_exit, args=(stop_event,), name="InputThread", daemon=True),
        threading.Thread(target=track_crypto_price, args=(symbol, interval, stop_event), name="PriceTracker", daemon=True),
    ]

    with graceful_shutdown(threads, stop_event):
        for thread in threads:
            thread.start()
        stop_event.wait()


if __name__ == "__main__":
    main()
