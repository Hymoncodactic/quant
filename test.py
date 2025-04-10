import alpaca_trade_api as tradeapi

# Use the correct base URL for sandbox data
api = tradeapi.REST(
    'CKXAKRD36DZVZ61XF43A',
    'mejXRL1kA2bXKX4DgsbgFfkBguqBBnN1esI87n4s',
    base_url='https://data.sandbox.alpaca.markets'
)

# Fetch historical data for TSLA
try:
    barset = api.get_bars('TSLA', 'minute', limit=1)  # Fetch the most recent minute bar
    price = barset[0].c  # Access the closing price of the first bar
    print(f"TSLA price: {price}")
except Exception as e:
    print(f"Error fetching data: {e}")