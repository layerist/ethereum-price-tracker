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
API_KEY: Final = "your_api_key"  # Replace with your actual CoinMarketCap API key
API_URL: Final = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_SYMBOL: Final = "ETH"
DEFAULT_CONVERT: Final = "USD"
DEFAULT_INTERVAL: Final = 5  # in seconds

HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": API_KEY,
}

# Fetch the cryptocurrency price from the API
def fetch_crypto_price(symbol: str = DEFAULT_SYMBOL, convert: str = DEFAULT_CONVERT) -> Optional[float]:
    params = {"symbol": symbol, "convert": convert}
    try:
        response = requests.get(API_URL, headers=HEADERS, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data["data"][symbol]["quote"][convert]["price"]
    except requests.RequestException as e:
        logging.error(f"Request error for {symbol}: {e}")
    except KeyError as e:
        logging.error(f"Response parsing error: Missing key {e}")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
    return None

# Track cryptocurrency price periodically
def track_crypto_price(
    symbol: str = DEFAULT_SYMBOL, 
    interval: int = DEFAULT_INTERVAL, 
    stop_event: threading.Event = None,
) -> None:
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

# Context manager for clean thread shutdown
@contextmanager
def graceful_shutdown(
    threads: List[threading.Thread], stop_event: threading.Event
) -> Generator[None, None, None]:
    try:
        yield
    finally:
        logging.info("Initiating shutdown. Stopping threads...")
        stop_event.set()
        for thread in threads:
            thread.join()
        logging.info("All threads successfully stopped.")

# Function to handle user-triggered shutdown
def stop_script(stop_event: threading.Event) -> None:
    input("Press Enter to stop the script...\n")
    stop_event.set()

# Entry point
if __name__ == "__main__":
    # Read arguments from the command line
    symbol = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL
    try:
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_INTERVAL
        if interval <= 0:
            raise ValueError("Interval must be a positive integer.")
    except ValueError:
        logging.warning("Invalid interval provided, using default value.")
        interval = DEFAULT_INTERVAL

    # Initialize threading and stop event
    stop_event = threading.Event()
    threads = [
        threading.Thread(target=stop_script, args=(stop_event,), daemon=True),
        threading.Thread(target=track_crypto_price, args=(symbol, interval, stop_event), daemon=True),
    ]

    with graceful_shutdown(threads, stop_event):
        for thread in threads:
            thread.start()

        # Keep the main thread alive until the stop event is triggered
        stop_event.wait()
