import backtrader as bt
import pandas as pd
import yfinance as yf

# Define a strategy class
class MyStrategy(bt.Strategy):
    def __init__(self):
        # Add a simple moving average indicator
        self.sma = bt.indicators.SimpleMovingAverage(self.data.close, period=20)
    
    def next(self):
        # Implement trading logic based on the moving average
        if self.data.close[0] > self.sma[0]:
            self.buy()  # Buy signal
        elif self.data.close[0] < self.sma[0]:
            self.sell()  # Sell signal

# Fetch data using yfinance
data = yf.download('AAPL', start='2010-01-01', end='2020-12-31', auto_adjust=False)

# Flatten multi-level column names (if any)
data.columns = data.columns.map(lambda x: x[0] if isinstance(x, tuple) else x)

# Ensure the data has the correct column names
data = data.rename(columns={
    'Open': 'open',
    'High': 'high',
    'Low': 'low',
    'Close': 'close',
    'Volume': 'volume',
    'Adj Close': 'adj_close'
})

# Convert the data to a Backtrader-compatible format
data_feed = bt.feeds.PandasData(dataname=data)

# Initialize the backtesting engine
cerebro = bt.Cerebro()

# Add data to the backtesting engine
cerebro.adddata(data_feed)

# Add strategy to the backtesting engine
cerebro.addstrategy(MyStrategy)

# Run the backtest
cerebro.run()

# Retrieve and print performance metrics
portfolio_value = cerebro.broker.getvalue()
print(f"Final Portfolio Value: ${portfolio_value:.2f}")

# Plot the results
cerebro.plot()