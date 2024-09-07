import requests
import time
import threading
import logging
import sys
from contextlib import contextmanager
from typing import Optional, List, Generator
from typing_extensions import Final

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
API_KEY: Final = 'your_api_key'  # Replace with your actual CoinMarketCap API key
URL: Final = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
DEFAULT_SYMBOL: Final = 'ETH'
DEFAULT_CONVERT: Final = 'USD'
DEFAULT_INTERVAL: Final = 5  # seconds

HEADERS = {
    'Accepts': 'application/json',
    'X-CMC_PRO_API_KEY': API_KEY
}

last_fetched_price: Optional[float] = None
stop_event: threading.Event = threading.Event()

# Function to fetch the price of a cryptocurrency
def fetch_crypto_price(symbol: str = DEFAULT_SYMBOL, convert: str = DEFAULT_CONVERT) -> Optional[float]:
    global last_fetched_price
    params = {'symbol': symbol, 'convert': convert}
    try:
        response = requests.get(URL, headers=HEADERS, params=params)
        response.raise_for_status()  # Raise an exception for HTTP errors
        data = response.json()
        price = data['data'][symbol]['quote'][convert]['price']
        last_fetched_price = price
        return price
    except requests.exceptions.RequestException as e:
        logging.error(f"HTTP request error: {e}")
    except KeyError as e:
        logging.error(f"Response parsing error: Missing key {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
    
    return last_fetched_price  # Return last known price if the current request fails

# Function to run the price fetching loop
def track_crypto_price(symbol: str = DEFAULT_SYMBOL, interval: int = DEFAULT_INTERVAL) -> None:
    while not stop_event.is_set():
        price = fetch_crypto_price(symbol=symbol)
        if price:
            logging.info(f"{symbol} price: ${price:.2f} {DEFAULT_CONVERT}")
        stop_event.wait(interval)  # More efficient than time.sleep()

# Context manager for thread management
@contextmanager
def graceful_shutdown(threads: List[threading.Thread]) -> Generator[None, None, None]:
    try:
        yield
    finally:
        logging.info("Shutdown initiated. Stopping threads...")
        stop_event.set()
        for thread in threads:
            thread.join()
        logging.info("All threads successfully stopped.")

# Function to handle stopping the script with keyboard input
def stop_script() -> None:
    input("Press Enter to stop the script...\n")
    stop_event.set()

if __name__ == "__main__":
    # Get symbol and interval from command-line arguments, if provided
    symbol = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL
    try:
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_INTERVAL
    except ValueError:
        logging.warning("Invalid interval provided, using default value.")
        interval = DEFAULT_INTERVAL

    threads = [
        threading.Thread(target=stop_script, daemon=True),
        threading.Thread(target=track_crypto_price, args=(symbol, interval), daemon=True)
    ]

    with graceful_shutdown(threads):
        for thread in threads:
            thread.start()

        # Keep the main thread alive until all threads finish
        stop_event.wait()
