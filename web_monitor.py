from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import asyncio
import json
from datetime import datetime
import pytz
from websockets import connect
from collections import deque
from typing import List
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Crypto Monitor", description="Real-time crypto trading monitor")

# Data storage (same as terminal version)
symbols = ['btcusdt', 'ethusdt', 'solusdt', 'bnbusdt', 'dogeusdt', 'wifusdt']
websocket_url_base = 'wss://fstream.binance.com/ws/'

funding_data = {symbol: None for symbol in symbols}
recent_liquidations = deque(maxlen=25)
recent_trades = deque(maxlen=30)
whale_alerts = deque(maxlen=15)

# Store connected WebSocket clients
connected_clients: List[WebSocket] = []

def format_usd(amount):
    """Format USD amount with appropriate suffixes"""
    if amount >= 1_000_000:
        return f"${amount/1_000_000:.2f}M"
    elif amount >= 1_000:
        return f"${amount/1_000:.1f}K"
    else:
        return f"${amount:.0f}"

def get_funding_style_class(yearly_rate):
    """Return CSS class based on funding rate"""
    if yearly_rate > 50:
        return "funding-extreme"
    elif yearly_rate > 30:
        return "funding-high"
    elif yearly_rate > 5:
        return "funding-positive"
    elif yearly_rate < -10:
        return "funding-negative"
    else:
        return "funding-normal"

async def broadcast_to_clients(data: dict):
    """Send data to all connected WebSocket clients"""
    if connected_clients:
        disconnected = []
        for client in connected_clients:
            try:
                await client.send_text(json.dumps(data))
            except Exception:
                disconnected.append(client)
        
        # Remove disconnected clients
        for client in disconnected:
            connected_clients.remove(client)

async def binance_funding_stream(symbol):
    """Connect to Binance WebSocket and stream funding data"""
    websocket_url = f'{websocket_url_base}{symbol}@markPrice'
    
    while True:
        try:
            async with connect(websocket_url) as websocket:
                while True:
                    try:
                        message = await websocket.recv()
                        data = json.loads(message)
                        
                        symbol_display = data['s'].replace('USDT', '').upper()
                        funding_rate = float(data['r']) * 100
                        yearly_funding_rate = funding_rate * 3 * 365
                        
                        funding_data[symbol] = {
                            'symbol_display': symbol_display,
                            'funding_rate': funding_rate,
                            'yearly_rate': yearly_funding_rate,
                            'style_class': get_funding_style_class(yearly_funding_rate)
                        }
                        
                        # Broadcast funding data update
                        await broadcast_to_clients({
                            'type': 'funding_update',
                            'data': funding_data
                        })
                        
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.error(f"Error in funding stream {symbol}: {e}")
                        break
                        
        except Exception as e:
            logger.error(f"Connection error for funding {symbol}: {e}")
            await asyncio.sleep(5)

async def binance_liquidation_stream():
    """Connect to Binance liquidation WebSocket"""
    websocket_url = 'wss://fstream.binance.com/ws/!forceOrder@arr'
    
    while True:
        try:
            async with connect(websocket_url) as websocket:
                while True:
                    try:
                        msg = await websocket.recv()
                        order_data = json.loads(msg)['o']
                        symbol = order_data['s'].replace('USDT', '')
                        side = order_data['S']
                        timestamp = int(order_data['T'])
                        filled_quantity = float(order_data['z'])
                        price = float(order_data['p'])
                        usd_size = filled_quantity * price
                        est = pytz.timezone("US/Eastern")
                        time_est = datetime.fromtimestamp(timestamp / 1000, est).strftime('%H:%M:%S')
                        
                        if usd_size >= 5000:
                            liquidation = {
                                'symbol': symbol[:4],
                                'side': side,
                                'side_text': 'LONG LIQ' if side == 'SELL' else 'SHORT LIQ',
                                'usd_size': format_usd(usd_size),
                                'time': time_est,
                                'color_class': 'liquidation-long' if side == 'SELL' else 'liquidation-short'
                            }
                            
                            recent_liquidations.appendleft(liquidation)
                            
                            await broadcast_to_clients({
                                'type': 'liquidation_update',
                                'data': list(recent_liquidations)
                            })
                            
                    except Exception as e:
                        logger.error(f"Error in liquidation stream: {e}")
                        break
                        
        except Exception as e:
            logger.error(f"Liquidation connection error: {e}")
            await asyncio.sleep(5)

async def binance_trade_stream(symbol):
    """Connect to Binance trade WebSocket"""
    websocket_url = f"{websocket_url_base}{symbol}@aggTrade"
    
    while True:
        try:
            async with connect(websocket_url) as websocket:
                while True:
                    try:
                        message = await websocket.recv()
                        data = json.loads(message)
                        
                        price = float(data['p'])
                        quantity = float(data['q'])
                        trade_time = int(data['T'])
                        is_buyer_maker = data['m']
                        
                        usd_size = price * quantity 
                        
                        if usd_size >= 15000:
                            trade_type = 'SELL' if is_buyer_maker else "BUY"
                            est = pytz.timezone('US/Eastern')
                            readable_trade_time = datetime.fromtimestamp(trade_time / 1000, est).strftime('%H:%M:%S')
                            display_symbol = symbol.upper().replace('USDT', '')
                            
                            trade = {
                                'symbol': display_symbol[:4],
                                'type': trade_type,
                                'usd_size': format_usd(usd_size),
                                'price': price,
                                'time': readable_trade_time,
                                'color_class': 'trade-buy' if trade_type == 'BUY' else 'trade-sell'
                            }
                            
                            recent_trades.appendleft(trade)
                            
                            # Also add to whale alerts if $100K+
                            if usd_size >= 100000:
                                whale_alert = trade.copy()
                                whale_alert['usd_value'] = usd_size
                                
                                # Determine whale size class
                                if usd_size >= 1000000:
                                    whale_alert['whale_class'] = 'whale-mega'
                                    whale_alert['size_indicator'] = 'MEGA'
                                elif usd_size >= 500000:
                                    whale_alert['whale_class'] = 'whale-huge'
                                    whale_alert['size_indicator'] = 'HUGE'
                                else:
                                    whale_alert['whale_class'] = 'whale-big'
                                    whale_alert['size_indicator'] = 'BIG'
                                
                                whale_alerts.appendleft(whale_alert)
                                
                                await broadcast_to_clients({
                                    'type': 'whale_alert_update',
                                    'data': list(whale_alerts)
                                })
                            
                            await broadcast_to_clients({
                                'type': 'trade_update', 
                                'data': list(recent_trades)
                            })
                            
                    except Exception as e:
                        logger.error(f"Error in trade stream {symbol}: {e}")
                        break
                        
        except Exception as e:
            logger.error(f"Trade connection error for {symbol}: {e}")
            await asyncio.sleep(5)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time data"""
    await websocket.accept()
    connected_clients.append(websocket)
    
    # Send initial data
    await websocket.send_text(json.dumps({
        'type': 'initial_data',
        'funding_data': funding_data,
        'liquidations': list(recent_liquidations),
        'trades': list(recent_trades),
        'whale_alerts': list(whale_alerts)
    }))
    
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.remove(websocket)

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Serve the main dashboard HTML"""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚀 Crypto Monitor</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            background: #0a0a0a;
            color: #ffffff;
            padding: 20px;
        }
        
        .header {
            text-align: center;
            padding: 20px;
            background: linear-gradient(135deg, #1e3c72, #2a5298);
            border-radius: 10px;
            margin-bottom: 20px;
        }
        
        .dashboard {
            display: grid;
            grid-template-columns: 1fr 1fr;
            grid-template-rows: auto auto;
            gap: 20px;
            height: calc(100vh - 200px);
        }
        
        .panel {
            background: #1a1a1a;
            border: 2px solid #333;
            border-radius: 10px;
            padding: 15px;
            overflow-y: auto;
        }
        
        .panel-title {
            font-size: 18px;
            font-weight: bold;
            margin-bottom: 15px;
            text-align: center;
            padding: 10px;
            border-radius: 5px;
        }
        
        .funding-title { background: #0066cc; }
        .liquidation-title { background: #cc6600; }
        .whale-title { background: #cc0066; }
        .trade-title { background: #00cc66; }
        
        /* Funding Styles */
        .funding-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .funding-table th,
        .funding-table td {
            padding: 8px;
            text-align: left;
            border-bottom: 1px solid #333;
        }
        
        .funding-extreme { background-color: #ff0000; color: white; }
        .funding-high { background-color: #ffaa00; color: black; }
        .funding-positive { background-color: #0066ff; color: white; }
        .funding-negative { background-color: #00aa00; color: white; }
        .funding-normal { background-color: #00ff00; color: black; }
        
        /* Liquidation Styles */
        .liquidation-item {
            padding: 5px;
            margin: 2px 0;
            border-radius: 3px;
        }
        
        .liquidation-long { color: #00ff00; }
        .liquidation-short { color: #ff4444; }
        
        /* Trade Styles */
        .trade-item, .whale-item {
            padding: 5px;
            margin: 2px 0;
            border-radius: 3px;
        }
        
        .trade-buy { color: #00ff00; }
        .trade-sell { color: #ff4444; }
        
        /* Whale Alert Styles */
        .whale-mega { 
            background: linear-gradient(45deg, #ff00ff, #8b00ff);
            animation: pulse 2s infinite;
        }
        .whale-huge { background: #8b00ff; }
        .whale-big { background: #00aaff; }
        
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.7; }
            100% { opacity: 1; }
        }
        
        .status {
            position: fixed;
            top: 10px;
            right: 10px;
            padding: 10px;
            border-radius: 5px;
        }
        
        .connected { background: #00aa00; }
        .disconnected { background: #aa0000; }
        
        .legend {
            background: #2a2a2a;
            padding: 10px;
            border-radius: 5px;
            margin-top: 20px;
            text-align: center;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="status" id="status">🔴 Connecting...</div>
    
    <div class="header">
        <h1>🚀 Unified Crypto Monitor - Real-time Dashboard</h1>
        <p>Funding Rates | Liquidations | Whale Trades</p>
    </div>
    
    <div class="dashboard">
        <div class="panel">
            <div class="panel-title funding-title">📊 Funding Rates</div>
            <table class="funding-table">
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Rate</th>
                        <th>Yearly</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="funding-data">
                </tbody>
            </table>
        </div>
        
        <div class="panel">
            <div class="panel-title liquidation-title">🔥 Recent Liquidations (5K+)</div>
            <div id="liquidations-data"></div>
        </div>
        
        <div class="panel">
            <div class="panel-title whale-title">🚨 MEGA WHALE ALERTS (100K+)</div>
            <div id="whale-alerts-data"></div>
        </div>
        
        <div class="panel">
            <div class="panel-title trade-title">🐋 Whale Trades (15K+)</div>
            <div id="trades-data"></div>
        </div>
    </div>
    
    <div class="legend">
        🟥 >50% | 🟨 >30% | 🟦 >5% | 🟩 <-10% | 💚 Normal | 📈 Long Liq | 📉 Short Liq | 🚨 Mega Whales 100K+ | 🐋 Whale Trades 15K+
    </div>

    <script>
        const ws = new WebSocket(`ws://${window.location.host}/ws`);
        const status = document.getElementById('status');
        
        ws.onopen = function() {
            status.textContent = '🟢 Connected';
            status.className = 'status connected';
        };
        
        ws.onclose = function() {
            status.textContent = '🔴 Disconnected';
            status.className = 'status disconnected';
        };
        
        ws.onmessage = function(event) {
            const data = JSON.parse(event.data);
            
            switch(data.type) {
                case 'initial_data':
                    updateFunding(data.funding_data);
                    updateLiquidations(data.liquidations);
                    updateTrades(data.trades);
                    updateWhaleAlerts(data.whale_alerts);
                    break;
                case 'funding_update':
                    updateFunding(data.data);
                    break;
                case 'liquidation_update':
                    updateLiquidations(data.data);
                    break;
                case 'trade_update':
                    updateTrades(data.data);
                    break;
                case 'whale_alert_update':
                    updateWhaleAlerts(data.data);
                    break;
            }
        };
        
        function updateFunding(fundingData) {
            const tbody = document.getElementById('funding-data');
            tbody.innerHTML = '';
            
            Object.entries(fundingData).forEach(([symbol, data]) => {
                if (data) {
                    const row = document.createElement('tr');
                    row.innerHTML = `
                        <td>${data.symbol_display}</td>
                        <td class="${data.style_class}">${data.funding_rate.toFixed(3)}%</td>
                        <td class="${data.style_class}">${data.yearly_rate > 0 ? '+' : ''}${data.yearly_rate.toFixed(1)}%</td>
                        <td>${getStatusEmoji(data.yearly_rate)}</td>
                    `;
                    tbody.appendChild(row);
                }
            });
        }
        
        function updateLiquidations(liquidations) {
            const container = document.getElementById('liquidations-data');
            container.innerHTML = liquidations.map(liq => `
                <div class="liquidation-item ${liq.color_class}">
                    ${liq.side === 'SELL' ? '📈' : '📉'} ${liq.symbol} ${liq.side_text} ${liq.usd_size} ${liq.time}
                </div>
            `).join('');
        }
        
        function updateTrades(trades) {
            const container = document.getElementById('trades-data');
            container.innerHTML = trades.map(trade => `
                <div class="trade-item ${trade.color_class}">
                    ${trade.type === 'BUY' ? '🟢' : '🔴'} ${trade.symbol} ${trade.type} ${trade.usd_size} @ $${trade.price.toFixed(2)} ${trade.time}
                </div>
            `).join('');
        }
        
        function updateWhaleAlerts(alerts) {
            const container = document.getElementById('whale-alerts-data');
            container.innerHTML = alerts.map(alert => `
                <div class="whale-item ${alert.whale_class} ${alert.color_class}">
                    ${getWhaleEmoji(alert.usd_value)} ${alert.size_indicator} ${alert.symbol} ${alert.type} ${alert.usd_size}<br>
                    &nbsp;&nbsp;&nbsp;&nbsp;@ $${alert.price.toFixed(2)} ${alert.time}
                </div>
            `).join('');
        }
        
        function getStatusEmoji(yearlyRate) {
            if (yearlyRate > 30) return '🔥 HIGH';
            if (yearlyRate > 5) return '📈 POS';
            if (yearlyRate < -10) return '📉 NEG';
            return '😐 NORM';
        }
        
        function getWhaleEmoji(usdValue) {
            if (usdValue >= 1000000) return '💎';
            if (usdValue >= 500000) return '🏦';
            return '⚡️';
        }
    </script>
</body>
</html>
"""

# Background tasks to start the WebSocket streams
@app.on_event("startup")
async def startup_event():
    """Start background tasks for WebSocket streams"""
    
    # Start funding rate streams
    for symbol in symbols:
        asyncio.create_task(binance_funding_stream(symbol))
    
    # Start liquidation stream
    asyncio.create_task(binance_liquidation_stream())
    
    # Start trade streams
    for symbol in symbols:
        asyncio.create_task(binance_trade_stream(symbol))
    
    logger.info("🚀 Crypto Monitor started - all WebSocket streams active")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)