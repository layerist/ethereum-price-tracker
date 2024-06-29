import requests
import time
import threading

# Replace 'your_api_key' with your actual CoinMarketCap API key
API_KEY = 'your_api_key'
URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
PARAMS = {
    'symbol': 'ETH',
    'convert': 'USD'
}
HEADERS = {
    'Accepts': 'application/json',
    'X-CMC_PRO_API_KEY': API_KEY
}

# Function to fetch the price of Ethereum
def fetch_eth_price():
    response = requests.get(URL, headers=HEADERS, params=PARAMS)
    data = response.json()
    eth_price = data['data']['ETH']['quote']['USD']['price']
    return eth_price

# Function to run the price fetching loop
def track_eth_price():
    while not stop_event.is_set():
        try:
            eth_price = fetch_eth_price()
            print(f"Ethereum (ETH) price: ${eth_price:.2f} USD")
        except Exception as e:
            print(f"An error occurred: {e}")
        time.sleep(1)

# Function to handle stopping the script with keyboard input
def stop_script():
    input("Press Enter to stop the script...\n")
    stop_event.set()

if __name__ == "__main__":
    stop_event = threading.Event()
    threading.Thread(target=stop_script).start()
    track_eth_price()
