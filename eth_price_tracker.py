import requests
import time
import threading
import logging
import sys
from contextlib import contextmanager
from typing import Optional, List, Generator
from typing_extensions import Final

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Constants
API_KEY: Final[str] = "your_api_key"  # Replace with your actual CoinMarketCap API key
API_URL: Final[str] = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_SYMBOL: Final[str] = "ETH"
DEFAULT_CONVERT: Final[str] = "USD"
DEFAULT_INTERVAL: Final[int] = 5  # in seconds
TIMEOUT: Final[int] = 10  # API request timeout in seconds

HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": API_KEY,
}

def fetch_crypto_price(symbol: str = DEFAULT_SYMBOL, convert: str = DEFAULT_CONVERT) -> Optional[float]:
    """Fetch the latest cryptocurrency price from the API."""
    params = {"symbol": symbol, "convert": convert}
    try:
        logging.debug(f"Fetching price for {symbol} in {convert}...")
        response = requests.get(API_URL, headers=HEADERS, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return data["data"].get(symbol, {}).get("quote", {}).get(convert, {}).get("price")
    except requests.Timeout:
        logging.error("Request timed out. Retrying...")
    except requests.RequestException as e:
        logging.error(f"API request error: {e}")
    except (KeyError, TypeError) as e:
        logging.error(f"Unexpected data format: {e}")
    return None

def track_crypto_price(symbol: str, interval: int, stop_event: threading.Event) -> None:
    """Periodically fetch and log the cryptocurrency price."""
    last_price: Optional[float] = None
    while not stop_event.is_set():
        price = fetch_crypto_price(symbol=symbol)
        if price is not None:
            logging.info(f"{symbol} price: ${price:.2f} {DEFAULT_CONVERT}")
            last_price = price
        elif last_price is not None:
            logging.warning(f"Using last known price: ${last_price:.2f} {DEFAULT_CONVERT}")
        else:
            logging.warning("Price data unavailable.")
        stop_event.wait(interval)

@contextmanager
def graceful_shutdown(threads: List[threading.Thread], stop_event: threading.Event) -> Generator[None, None, None]:
    """Ensure clean shutdown of threads on exit."""
    try:
        yield
    finally:
        logging.info("Initiating shutdown. Stopping threads...")
        stop_event.set()
        for thread in threads:
            thread.join()
        logging.info("All threads successfully stopped.")

def stop_script(stop_event: threading.Event) -> None:
    """Wait for user input to stop the script."""
    input("Press Enter to stop the script...\n")
    stop_event.set()

if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL
    try:
        interval = max(1, int(sys.argv[2])) if len(sys.argv) > 2 else DEFAULT_INTERVAL
    except ValueError:
        logging.warning("Invalid interval provided. Using default value.")
        interval = DEFAULT_INTERVAL

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=stop_script, args=(stop_event,), daemon=True),
        threading.Thread(target=track_crypto_price, args=(symbol, interval, stop_event), daemon=True),
    ]

    with graceful_shutdown(threads, stop_event):
        for thread in threads:
            thread.start()
        stop_event.wait()
