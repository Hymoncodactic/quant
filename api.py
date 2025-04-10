import requests
import json
import os
import matplotlib.pyplot as plt
import time


# set up security headers
headers = {"Authorization": "21738854ZvLKtvghTMiURHtBemakcNbYFapmJ"}


# fetch exchange and instrument
# def exchage_data(headers):
#     url = "https://live.trading212.com/api/v0/equity/metadata/exchanges"
#     response = requests.get(url, headers=headers)
#     return response.json() if response.status_code == 200 else print("Failed to fetch exchange data")


# def fetch_instruments(headers, retries=5, code='TSLA'):
#     url = "https://live.trading212.com/api/v0/equity/metadata/instruments"
#     for i in range(retries):
#         response = requests.get(url, headers=headers)
#         if response.status_code == 200:
#             return response.json()
#         elif response.status_code == 429:
#             wait = 2 ** i
#             print(f"Rate limit hit. Retrying in {wait} seconds...")
#             time.sleep(wait)
#         else:
#             print(f"status:{response.status_code} Failed to fetch instrument data")
#             return None
#     print("Max retries reached. Could not fetch instrument data.")
#     return None

# fetch & manage Orders
def fetch_all_orders(headers):
    url = "https://live.trading212.com/api/v0/equity/orders"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else None

# def place_limit_order(headers, data):
#     url = "https://live.trading212.com/api/v0/equity/orders/limit"
#     response = requests.post(url, headers=headers, json=data)
#     return response.json() if response.status_code == 200 else None

# def place_market_order(headers, data):
#     url = "https://live.trading212.com/api/v0/equity/orders/market"
#     response = requests.post(url, headers=headers, json=data)
#     return response.json() if response.status_code == 200 else None

# def place_stop_order(headers, data):
#     url = "https://live.trading212.com/api/v0/equity/orders/stop"
#     response = requests.post(url, headers=headers, json=data)
#     return response.json() if response.status_code == 200 else None

# def place_stop_limit_order(headers, data):
#     url = "https://live.trading212.com/api/v0/equity/orders/stop_limit"
#     response = requests.post(url, headers=headers, json=data)
#     return response.json() if response.status_code == 200 else None

# def cancel_order_by_id(headers, order_id):
#     url = f"https://live.trading212.com/api/v0/equity/orders/{order_id}"
#     response = requests.delete(url, headers=headers)
#     return response.json() if response.status_code == 200 else None

# def fetch_order_by_id(headers, order_id):
#     url = f"https://live.trading212.com/api/v0/equity/orders/{order_id}"
#     response = requests.get(url, headers=headers)
#     return response.json() if response.status_code == 200 else None


# # fetch account info
# def fetch_account_cash(headers):
#     url = "https://live.trading212.com/api/v0/equity/account/cash"
#     response = requests.get(url, headers=headers)
#     return response.json() if response.status_code == 200 else None

# def fetch_account_info(headers):
#     url = "https://live.trading212.com/api/v0/equity/account/info"
#     response = requests.get(url, headers=headers)
#     return response.json() if response.status_code == 200 else None



# Extract the relevant data
os.makedirs("datafiles", exist_ok=True)  # Ensure the 'datafiles' folder exists

# Specify the full path to the file inside the 'datafiles' folder
file_path = os.path.join("datafiles", "orders.json")

# Write the JSON data to the file
with open(file_path, "w") as json_file:
    json.dump(fetch_all_orders(headers), json_file, indent=4)  # Use indent=4 for pretty formatting

print(f"Data saved to {file_path}")




# def get_tsla_price_with_backoff(headers, retries=5):
#     url = "https://live.trading212.com/api/v0/equity/metadata/instruments"
#     for i in range(retries):
#         response = requests.get(url, headers=headers)
#         if response.status_code == 200:
#             data = response.json()
#             for instrument in data:
#                 if instrument.get("ticker", "").startswith("TSLA"):
#                     return float(instrument["lastPrice"])
#             print("TSLA instrument not found in response")
#             return None
#         elif response.status_code == 429:
#             print(f"Rate limit hit. Retrying in {2 * i} seconds...")
#             time.sleep(2 * i)  # Exponential backoff
#         else:
#             print(f"Failed to fetch TSLA price. Status code: {response.status_code}")
#             print("Response:", response.text)
#             return None
#     print("Max retries reached. Could not fetch TSLA price.")
#     return None

# def simple_strategy(current_price, last_price):
#     # Example logic: Buy if price jumps by more than 2%
#     return current_price > last_price * 1.02

# # Fetch a list of recent TSLA prices (simulate for demo)
# price_history = []

# for _ in range(10):  # simulate 10 minutes of price updates
#     price = get_tsla_price_with_backoff(headers)
#     if price:
#         price_history.append(price)
#     time.sleep(1)  # simulate 1-minute intervals (shortened here for testing)

# # Run strategy check
# if len(price_history) >= 2:
#     last_price = price_history[-2]
#     current_price = price_history[-1]
#     print(f"Last price: {last_price}, Current price: {current_price}")
#     if simple_strategy(current_price, last_price):
#         print("💡 Buy Signal")
#     else:
#         print("🟡 Hold")

# # Plot the price trend
# plt.figure(figsize=(10, 5))
# plt.plot(price_history, marker='o')
# plt.title("TSLA Price Trend")
# plt.xlabel("Time (simulated steps)")
# plt.ylabel("Price")
# plt.grid(True)
# plt.tight_layout()
# plt.savefig("datafiles/tsla_price_trend.png")
# print("Price trend graph saved to datafiles/tsla_price_trend.png")
# plt.show()