import requests
import json
import os


# set up security headers
headers = {"Authorization": "21738854ZvLKtvghTMiURHtBemakcNbYFapmJ"}


# fetch exchange and instrument
def exchage_data(headers):
    url = "https://live.trading212.com/api/v0/equity/metadata/exchanges"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else None

def fetch_instruments(headers):
    url = "https://live.trading212.com/api/v0/equity/metadata/instruments"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else None



# fetch & manage Orders
def fetch_all_orders(headers):
    url = "https://live.trading212.com/api/v0/equity/orders"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else None

def place_limit_order(headers, data):
    url = "https://live.trading212.com/api/v0/equity/orders/limit"
    response = requests.post(url, headers=headers, json=data)
    return response.json() if response.status_code == 200 else None

def place_market_order(headers, data):
    url = "https://live.trading212.com/api/v0/equity/orders/market"
    response = requests.post(url, headers=headers, json=data)
    return response.json() if response.status_code == 200 else None

def place_stop_order(headers, data):
    url = "https://live.trading212.com/api/v0/equity/orders/stop"
    response = requests.post(url, headers=headers, json=data)
    return response.json() if response.status_code == 200 else None

def place_stop_limit_order(headers, data):
    url = "https://live.trading212.com/api/v0/equity/orders/stop_limit"
    response = requests.post(url, headers=headers, json=data)
    return response.json() if response.status_code == 200 else None

def cancel_order_by_id(headers, order_id):
    url = f"https://live.trading212.com/api/v0/equity/orders/{order_id}"
    response = requests.delete(url, headers=headers)
    return response.json() if response.status_code == 200 else None

def fetch_order_by_id(headers, order_id):
    url = f"https://live.trading212.com/api/v0/equity/orders/{order_id}"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else None


# fetch account info
def fetch_account_cash(headers):
    url = "https://live.trading212.com/api/v0/equity/account/cash"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else None

def fetch_account_info(headers):
    url = "https://live.trading212.com/api/v0/equity/account/info"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else None



# Extract the relevant data
os.makedirs("datafiles", exist_ok=True)  # Ensure the 'datafiles' folder exists

# Specify the full path to the file inside the 'datafiles' folder
file_path = os.path.join("datafiles", "exchange_data.json")

# Write the JSON data to the file
with open(file_path, "w") as json_file:
    json.dump(exchage_data(headers), json_file, indent=4)  # Use indent=4 for pretty formatting

print(f"Data saved to {file_path}")
