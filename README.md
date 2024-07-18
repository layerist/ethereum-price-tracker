# Ethereum Price Tracker

This script continuously tracks and prints the price of Ethereum (ETH) in USD using the CoinMarketCap API.

## Features

- Fetches the current price of Ethereum every 5 seconds.
- Handles potential API request errors gracefully.
- Allows the user to stop the script with a simple keyboard input.

## Prerequisites

- Python 3.x
- `requests` library (install with `pip install requests`)
- A valid CoinMarketCap API key

## Installation

1. Clone the repository:
    ```sh
    git clone https://github.com/layerist/eth-price-tracker.git
    cd eth-price-tracker
    ```

2. Install the required packages:
    ```sh
    pip install requests
    ```

3. Replace `'your_api_key'` in the script with your actual CoinMarketCap API key.

## Usage

Run the script:
```sh
python eth_price_tracker.py
```

To stop the script, press Enter in the terminal where the script is running.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
