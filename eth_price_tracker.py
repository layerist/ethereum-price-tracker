import requests
import time
import threading
import logging
import sys
from contextlib import contextmanager

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Replace 'your_api_key' with your actual CoinMarketCap API key
API_KEY = 'your_api_key'
URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
DEFAULT_SYMBOL = 'ETH'
DEFAULT_CONVERT = 'USD'
DEFAULT_INTERVAL = 5  # seconds

HEADERS = {
    'Accepts': 'application/json',
    'X-CMC_PRO_API_KEY': API_KEY
}

last_fetched_price = None

# Function to fetch the price of a cryptocurrency
def fetch_crypto_price(symbol=DEFAULT_SYMBOL, convert=DEFAULT_CONVERT):
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
        logging.error(f"Request error: {e}")
    except KeyError as e:
        logging.error(f"Parsing error: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
    return last_fetched_price  # Return last known price if current request fails

# Function to run the price fetching loop
def track_crypto_price(symbol=DEFAULT_SYMBOL, interval=DEFAULT_INTERVAL):
    while not stop_event.is_set():
        price = fetch_crypto_price(symbol=symbol)
        if price:
            logging.info(f"{symbol} price: ${price:.2f} {DEFAULT_CONVERT}")
        time.sleep(interval)

# Context manager for thread management
@contextmanager
def graceful_shutdown(threads):
    try:
        yield
    finally:
        stop_event.set()
        for thread in threads:
            thread.join()

# Function to handle stopping the script with keyboard input
def stop_script():
    input("Press Enter to stop the script...\n")
    stop_event.set()

if __name__ == "__main__":
    # Get symbol and interval from command-line arguments, if provided
    symbol = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_INTERVAL

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=stop_script),
        threading.Thread(target=track_crypto_price, args=(symbol, interval))
    ]

    with graceful_shutdown(threads):
        for thread in threads:
            thread.start()
