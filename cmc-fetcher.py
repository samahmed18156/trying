import os
import requests

# Set your API key directly or via environment variable
CMC_API_KEY = os.getenv("CMC_API_KEY", "a400d3b15907485fb5ff7820b43af26d")

BASE_URL = "https://pro-api.coinmarketcap.com"


def get_eth_usdt_price():
    url = f"{BASE_URL}/v1/cryptocurrency/quotes/latest"

    headers = {
        "X-CMC_PRO_API_KEY": CMC_API_KEY,
        "Accept": "application/json"
    }

    params = {
        "symbol": "ETH",
        "convert": "USDT"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        # Parse price from CoinMarketCap response structure
        price = data["data"]["ETH"]["quote"]["USDT"]["price"]
        return float(price)

    except requests.exceptions.RequestException as e:
        print(f"CoinMarketCap API error: {e}")
        return None
    except (KeyError, TypeError, ValueError) as e:
        print(f"Data parsing error: {e}")
        return None


if __name__ == "__main__":
    print("Starting CoinMarketCap price check...")
    price = get_eth_usdt_price()

    if price is not None:
        print(f"1 ETH = {price:.4f} USDT")
    else:
        print("Could not get ETH price.")