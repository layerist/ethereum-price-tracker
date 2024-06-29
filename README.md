# Ethereum Price Tracker

This script fetches and displays the price of Ethereum (ETH) in USD every second using the CoinMarketCap API. It runs in an infinite loop until the user stops it with a keyboard input.

## Requirements

- Python 3.x
- `requests` library

You can install the `requests` library using pip:

```bash
pip install requests
```

## Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/layerist/eth-price-tracker.git
   cd eth-price-tracker
   ```

2. **Get a CoinMarketCap API key:**

   Sign up at [CoinMarketCap](https://coinmarketcap.com/api/) to get your API key.

3. **Update the script with your API key:**

   Replace `'your_api_key'` in `eth_price_tracker.py` with your actual CoinMarketCap API key.

## Usage

Run the script:

```bash
python eth_price_tracker.py
```

The script will fetch and print the price of Ethereum in USD every second. To stop the script, press `Enter`.

## Example Output

```text
Ethereum (ETH) price: $1800.23 USD
Ethereum (ETH) price: $1799.87 USD
Ethereum (ETH) price: $1801.45 USD
...
Press Enter to stop the script...
```

## License

This project is licensed under the MIT License.
```
