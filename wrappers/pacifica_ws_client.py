"""
Pacifica WebSocket Client

Provides real-time data fetching for:
- prices (all symbols: mark, mid, oracle, funding, etc.)
- orderbook (per symbol with aggregation level)
- account_info (user collateral/balance) - requires auth
- account_positions (user positions) - requires auth
- account_orders (user open orders) - requires auth

WS URL: wss://ws.pacifica.fi/ws
Heartbeat: ping every 50s (timeout at 60s)
"""
import asyncio
import json
import time
import uuid
from typing import Optional, Dict, Any, Set, List
from dataclasses import dataclass, field

import websockets
from websockets.client import WebSocketClientProtocol

from mpdex.utils.common_pacifica import sign_message
from solders.keypair import Keypair


PACIFICA_WS_URL = "wss://ws.pacifica.fi/ws"
PING_INTERVAL = 50  # seconds (server timeout at 60s)


@dataclass
class PacificaWSClient:
    """
    Single WebSocket connection to Pacifica.
    Manages subscriptions and caches data.
    """
    ws_url: str = PACIFICA_WS_URL

    # Auth info (for trading via WS)
    public_key: Optional[str] = None
    agent_public_key: Optional[str] = None
    agent_keypair: Optional[Keypair] = None

    # Connection
    _ws: Optional[WebSocketClientProtocol] = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)
    _recv_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _ping_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Subscriptions
    _prices_subscribed: bool = field(default=False, repr=False)
    _orderbook_subs: Set[str] = field(default_factory=set, repr=False)  # symbol -> agg_level
    _account_info_subscribed: bool = field(default=False, repr=False)
    _account_positions_subscribed: bool = field(default=False, repr=False)
    _account_orders_subscribed: bool = field(default=False, repr=False)

    # Cached data
    _prices: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)  # symbol -> {mark, mid, oracle, ...}
    _orderbooks: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)  # symbol -> {bids, asks}
    _account_info: Optional[Dict[str, Any]] = field(default=None, repr=False)
    _positions: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)  # symbol -> position
    _orders: List[Dict[str, Any]] = field(default_factory=list, repr=False)  # all open orders

    # Events for waiting
    _prices_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _orderbook_events: Dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _account_info_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _positions_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _orders_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    # Pending requests (for trading)
    _pending_requests: Dict[str, asyncio.Future] = field(default_factory=dict, repr=False)

    async def connect(self) -> bool:
        """Connect to WebSocket"""
        async with self._lock:
            if self._ws is not None and self._running:
                return True

            try:
                self._ws = await websockets.connect(
                    self.ws_url,
                    ping_interval=None,  # We handle ping manually
                    ping_timeout=None,
                    close_timeout=5,
                )
                self._running = True
                self._recv_task = asyncio.create_task(self._recv_loop())
                self._ping_task = asyncio.create_task(self._ping_loop())
                return True
            except Exception as e:
                print(f"[pacifica_ws] connect failed: {e}")
                return False

    async def close(self):
        """Close WebSocket connection"""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        # Reset subscription states
        self._prices_subscribed = False
        self._orderbook_subs.clear()
        self._account_info_subscribed = False
        self._account_positions_subscribed = False
        self._account_orders_subscribed = False

    async def _ping_loop(self):
        """Send periodic ping to keep connection alive"""
        try:
            while self._running:
                await asyncio.sleep(PING_INTERVAL)
                if self._ws and self._running:
                    try:
                        await self._ws.send(json.dumps({"method": "ping"}))
                    except Exception as e:
                        print(f"[pacifica_ws] ping failed: {e}")
        except asyncio.CancelledError:
            pass

    async def _recv_loop(self):
        """Background task to receive messages"""
        while self._running and self._ws:
            try:
                msg = await self._ws.recv()
                data = json.loads(msg)
                await self._handle_message(data)
            except websockets.ConnectionClosed:
                print("[pacifica_ws] connection closed")
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[pacifica_ws] recv error: {e}")
                await asyncio.sleep(0.1)

    async def _handle_message(self, data: Dict[str, Any]):
        """Handle incoming WebSocket message"""
        channel = data.get("channel")

        # Pong response
        if channel == "pong":
            return

        # Subscribe confirmation
        if channel == "subscribe":
            return

        # Prices (all symbols)
        if channel == "prices":
            self._handle_prices(data.get("data", []))
            return

        # Orderbook
        if channel == "book":
            self._handle_orderbook(data.get("data", {}))
            return

        # Account info (collateral/balance)
        if channel == "account_info":
            self._handle_account_info(data.get("data", {}))
            return

        # Account positions
        if channel == "account_positions":
            self._handle_positions(data.get("data", []))
            return

        # Account orders
        if channel == "account_orders":
            self._handle_orders(data.get("data", []))
            return

        # Trading responses (create_order, cancel_order, etc.)
        if channel in ("create_order", "create_market_order", "cancel_order", "cancel_all_orders"):
            self._handle_trading_response(data)
            return

        # Response to requests (has 'id' and 'type')
        if "id" in data and "type" in data:
            self._handle_trading_response(data)
            return

    def _handle_prices(self, items: List[Dict[str, Any]]):
        """Handle prices channel data"""
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            self._prices[symbol] = {
                "mark": item.get("mark"),
                "mid": item.get("mid"),
                "oracle": item.get("oracle"),
                "funding": item.get("funding"),
                "next_funding": item.get("next_funding"),
                "open_interest": item.get("open_interest"),
                "volume_24h": item.get("volume_24h"),
                "yesterday_price": item.get("yesterday_price"),
                "timestamp": item.get("timestamp", int(time.time() * 1000)),
            }
        if not self._prices_event.is_set():
            self._prices_event.set()

    def _handle_orderbook(self, data: Dict[str, Any]):
        """
        Handle orderbook data.
        Format: {"l": [[bids], [asks]], "s": "SOL", "t": timestamp}
        """
        symbol = str(data.get("s", "")).upper()
        if not symbol:
            return

        levels = data.get("l", [[], []])
        bids_raw = levels[0] if len(levels) > 0 else []
        asks_raw = levels[1] if len(levels) > 1 else []

        def parse_levels(items: List[Dict]) -> List[List[float]]:
            result = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    price = float(item.get("p", 0))
                    amount = float(item.get("a", 0))
                    result.append([price, amount])
                except (ValueError, TypeError):
                    continue
            return result

        self._orderbooks[symbol] = {
            "bids": parse_levels(bids_raw),
            "asks": parse_levels(asks_raw),
            "time": data.get("t", int(time.time() * 1000)),
        }

        if symbol in self._orderbook_events:
            self._orderbook_events[symbol].set()

    def _handle_account_info(self, data: Dict[str, Any]):
        """
        Handle account_info data.
        Format: {ae, as, aw, b, f, mu, cm, oc, pb, pc, sc, t}
        """
        self._account_info = {
            "account_equity": data.get("ae"),
            "available_to_spend": data.get("as"),
            "available_to_withdraw": data.get("aw"),
            "balance": data.get("b"),
            "fee_tier": data.get("f"),
            "margin_used": data.get("mu"),
            "cross_maintenance_margin": data.get("cm"),
            "orders_count": data.get("oc"),
            "pending_balance": data.get("pb"),
            "positions_count": data.get("pc"),
            "stop_orders_count": data.get("sc"),
            "timestamp": data.get("t", int(time.time() * 1000)),
        }
        if not self._account_info_event.is_set():
            self._account_info_event.set()

    def _handle_positions(self, items: List[Dict[str, Any]]):
        """
        Handle account_positions data.
        Format: [{s, d, a, p, m, f, i, l, t}, ...]
        """
        # Clear existing positions and rebuild from snapshot
        self._positions.clear()

        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("s", "")).upper()
            if not symbol:
                continue

            amount = item.get("a", "0")
            try:
                if float(amount) == 0:
                    continue
            except (ValueError, TypeError):
                continue

            self._positions[symbol] = {
                "symbol": symbol,
                "side": item.get("d"),  # bid or ask
                "amount": amount,
                "entry_price": item.get("p"),
                "margin": item.get("m"),
                "funding_fee": item.get("f"),
                "is_isolated": item.get("i", False),
                "liquidation_price": item.get("l"),
                "timestamp": item.get("t", int(time.time() * 1000)),
            }

        if not self._positions_event.is_set():
            self._positions_event.set()

    def _handle_orders(self, items: List[Dict[str, Any]]):
        """
        Handle account_orders data.
        Format: [{i, I, s, d, p, a, f, c, t, st, ot, sp, ro}, ...]
        """
        parsed = []
        for item in items:
            if not isinstance(item, dict):
                continue
            parsed.append({
                "order_id": item.get("i"),
                "client_order_id": item.get("I"),
                "symbol": str(item.get("s", "")).upper(),
                "side": item.get("d"),  # bid or ask
                "price": item.get("p"),
                "amount": item.get("a"),
                "filled_amount": item.get("f"),
                "cancelled_amount": item.get("c"),
                "timestamp": item.get("t"),
                "stop_type": item.get("st"),
                "order_type": item.get("ot"),
                "stop_price": item.get("sp"),
                "reduce_only": item.get("ro", False),
            })
        self._orders = parsed
        if not self._orders_event.is_set():
            self._orders_event.set()

    def _handle_trading_response(self, data: Dict[str, Any]):
        """Handle trading response (create_order, cancel_order, etc.)"""
        req_id = data.get("id")
        if req_id and req_id in self._pending_requests:
            fut = self._pending_requests.pop(req_id)
            if not fut.done():
                fut.set_result(data)

    async def _send(self, msg: Dict[str, Any]):
        """Send message to WebSocket"""
        if not self._ws or not self._running:
            await self.connect()
        if self._ws:
            await self._ws.send(json.dumps(msg))

    # ----------------------------
    # Public Subscriptions
    # ----------------------------
    async def subscribe_prices(self):
        """Subscribe to prices channel (all symbols)"""
        if self._prices_subscribed:
            return
        await self._send({
            "method": "subscribe",
            "params": {"source": "prices"}
        })
        self._prices_subscribed = True

    async def unsubscribe_prices(self):
        """Unsubscribe from prices channel"""
        if not self._prices_subscribed:
            return
        await self._send({
            "method": "unsubscribe",
            "params": {"source": "prices"}
        })
        self._prices_subscribed = False

    async def subscribe_orderbook(self, symbol: str, agg_level: int = 1):
        """
        Subscribe to orderbook channel for symbol.
        agg_level: 1, 2, 5, 10, 100, 1000
        """
        symbol = symbol.upper()
        if symbol in self._orderbook_subs:
            return
        await self._send({
            "method": "subscribe",
            "params": {
                "source": "book",
                "symbol": symbol,
                "agg_level": agg_level,
            }
        })
        self._orderbook_subs.add(symbol)
        if symbol not in self._orderbook_events:
            self._orderbook_events[symbol] = asyncio.Event()

    async def unsubscribe_orderbook(self, symbol: str):
        """Unsubscribe from orderbook channel"""
        symbol = symbol.upper()
        if symbol not in self._orderbook_subs:
            return True
        await self._send({
            "method": "unsubscribe",
            "params": {
                "source": "book",
                "symbol": symbol,
            }
        })
        self._orderbook_subs.discard(symbol)
        return True

    # ----------------------------
    # Private Subscriptions
    # ----------------------------
    async def subscribe_account_info(self, account: str):
        """Subscribe to account_info channel (requires auth)"""
        if self._account_info_subscribed:
            return
        await self._send({
            "method": "subscribe",
            "params": {
                "source": "account_info",
                "account": account,
            }
        })
        self._account_info_subscribed = True

    async def subscribe_account_positions(self, account: str):
        """Subscribe to account_positions channel (requires auth)"""
        if self._account_positions_subscribed:
            return
        await self._send({
            "method": "subscribe",
            "params": {
                "source": "account_positions",
                "account": account,
            }
        })
        self._account_positions_subscribed = True

    async def subscribe_account_orders(self, account: str):
        """Subscribe to account_orders channel (requires auth)"""
        if self._account_orders_subscribed:
            return
        await self._send({
            "method": "subscribe",
            "params": {
                "source": "account_orders",
                "account": account,
            }
        })
        self._account_orders_subscribed = True

    async def subscribe_all_private(self, account: str):
        """Subscribe to all private channels"""
        await self.subscribe_account_info(account)
        await self.subscribe_account_positions(account)
        await self.subscribe_account_orders(account)

    # ----------------------------
    # Data Getters
    # ----------------------------
    def get_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached price data for symbol"""
        return self._prices.get(symbol.upper())

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get mark price for symbol"""
        price_data = self._prices.get(symbol.upper())
        if price_data:
            mark = price_data.get("mark")
            if mark is not None:
                try:
                    return float(mark)
                except (ValueError, TypeError):
                    pass
            # Fallback to mid, then oracle
            for key in ("mid", "oracle"):
                val = price_data.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass
        return None

    def get_all_prices(self) -> Dict[str, float]:
        """Get all mark prices {symbol: mark_price}"""
        result = {}
        for symbol, data in self._prices.items():
            mark = data.get("mark")
            if mark is not None:
                try:
                    result[symbol] = float(mark)
                except (ValueError, TypeError):
                    pass
        return result

    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached orderbook for symbol"""
        return self._orderbooks.get(symbol.upper())

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Get cached account info"""
        return self._account_info

    def get_collateral(self) -> Dict[str, Any]:
        """Get collateral info from cached account_info"""
        if not self._account_info:
            return {}
        return {
            "total_collateral": self._account_info.get("account_equity"),
            "available_collateral": self._account_info.get("available_to_spend"),
            "available_to_withdraw": self._account_info.get("available_to_withdraw"),
            "balance": self._account_info.get("balance"),
            "margin_used": self._account_info.get("margin_used"),
        }

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached position for symbol"""
        return self._positions.get(symbol.upper())

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached positions"""
        return dict(self._positions)

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get cached open orders, optionally filtered by symbol"""
        if symbol is None:
            return list(self._orders)
        symbol = symbol.upper()
        return [o for o in self._orders if o.get("symbol") == symbol]

    # ----------------------------
    # Wait for data
    # ----------------------------
    async def wait_prices_ready(self, timeout: float = 5.0) -> bool:
        """Wait until prices data is available"""
        if self._prices:
            return True
        try:
            await asyncio.wait_for(self._prices_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orderbook_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """Wait until orderbook data is available"""
        symbol = symbol.upper()
        if symbol in self._orderbooks:
            return True
        if symbol not in self._orderbook_events:
            self._orderbook_events[symbol] = asyncio.Event()
        try:
            await asyncio.wait_for(self._orderbook_events[symbol].wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_account_info_ready(self, timeout: float = 5.0) -> bool:
        """Wait until account_info data is available"""
        if self._account_info:
            return True
        try:
            await asyncio.wait_for(self._account_info_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_positions_ready(self, timeout: float = 5.0) -> bool:
        """Wait until positions data is available"""
        if self._positions_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._positions_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orders_ready(self, timeout: float = 5.0) -> bool:
        """Wait until orders data is available"""
        if self._orders_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._orders_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ----------------------------
    # Trading via WebSocket
    # ----------------------------
    async def create_order_ws(
        self,
        symbol: str,
        side: str,
        amount: str,
        price: Optional[str] = None,
        reduce_only: bool = False,
        slippage_percent: str = "0.1",
        client_order_id: Optional[str] = None,
        tif: str = "GTC",
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Create order via WebSocket.

        Args:
            symbol: Trading symbol (e.g., "BTC")
            side: "bid" or "ask"
            amount: Order amount
            price: Limit price (None for market order)
            reduce_only: Reduce only flag
            slippage_percent: Slippage for market orders
            client_order_id: Custom order ID
            tif: Time in force for limit orders

        Returns:
            Response dict with order_id
        """
        if not self.public_key or not self.agent_keypair:
            raise ValueError("Auth required for trading")

        req_id = str(uuid.uuid4())
        cloid = client_order_id or str(uuid.uuid4())
        ts = int(time.time() * 1000)

        if price is None:
            # Market order
            order_type = "create_market_order"
            signature_header = {
                "timestamp": ts,
                "expiry_window": 5000,
                "type": "create_market_order",
            }
            signature_payload = {
                "symbol": symbol.upper(),
                "reduce_only": reduce_only,
                "amount": amount,
                "side": side,
                "slippage_percent": slippage_percent,
                "client_order_id": cloid,
            }
        else:
            # Limit order
            order_type = "create_order"
            signature_header = {
                "timestamp": ts,
                "expiry_window": 5000,
                "type": "create_order",
            }
            signature_payload = {
                "symbol": symbol.upper(),
                "reduce_only": reduce_only,
                "amount": amount,
                "side": side,
                "price": price,
                "tif": tif,
                "client_order_id": cloid,
            }

        _, signature = sign_message(signature_header, signature_payload, self.agent_keypair)

        request = {
            "id": req_id,
            "params": {
                order_type: {
                    "account": self.public_key,
                    "agent_wallet": self.agent_public_key,
                    "signature": signature,
                    "timestamp": ts,
                    "expiry_window": 5000,
                    **signature_payload,
                }
            }
        }

        # Create future for response
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = fut

        try:
            await self._send(request)
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise TimeoutError(f"Order request timed out: {req_id}")

    async def cancel_order_ws(
        self,
        symbol: str,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Cancel order via WebSocket.

        Args:
            symbol: Trading symbol
            order_id: Exchange order ID
            client_order_id: Client order ID

        Returns:
            Response dict
        """
        if not self.public_key or not self.agent_keypair:
            raise ValueError("Auth required for trading")

        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id required")

        req_id = str(uuid.uuid4())
        ts = int(time.time() * 1000)

        signature_header = {
            "timestamp": ts,
            "expiry_window": 5000,
            "type": "cancel_order",
        }
        signature_payload: Dict[str, Any] = {
            "symbol": symbol.upper(),
        }
        if order_id:
            signature_payload["order_id"] = order_id
        if client_order_id:
            signature_payload["client_order_id"] = client_order_id

        _, signature = sign_message(signature_header, signature_payload, self.agent_keypair)

        request = {
            "id": req_id,
            "params": {
                "cancel_order": {
                    "account": self.public_key,
                    "agent_wallet": self.agent_public_key,
                    "signature": signature,
                    "timestamp": ts,
                    "expiry_window": 5000,
                    **signature_payload,
                }
            }
        }

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = fut

        try:
            await self._send(request)
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise TimeoutError(f"Cancel request timed out: {req_id}")

    async def cancel_all_orders_ws(
        self,
        symbol: Optional[str] = None,
        exclude_reduce_only: bool = False,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Cancel all orders via WebSocket.

        Args:
            symbol: Trading symbol (None for all symbols)
            exclude_reduce_only: Exclude reduce-only orders

        Returns:
            Response dict with cancelled_count
        """
        if not self.public_key or not self.agent_keypair:
            raise ValueError("Auth required for trading")

        req_id = str(uuid.uuid4())
        ts = int(time.time() * 1000)

        signature_header = {
            "timestamp": ts,
            "expiry_window": 5000,
            "type": "cancel_all_orders",
        }
        signature_payload: Dict[str, Any] = {
            "all_symbols": symbol is None,
            "exclude_reduce_only": exclude_reduce_only,
        }
        if symbol:
            signature_payload["symbol"] = symbol.upper()

        _, signature = sign_message(signature_header, signature_payload, self.agent_keypair)

        request = {
            "id": req_id,
            "params": {
                "cancel_all_orders": {
                    "account": self.public_key,
                    "agent_wallet": self.agent_public_key,
                    "signature": signature,
                    "timestamp": ts,
                    "expiry_window": 5000,
                    **signature_payload,
                }
            }
        }

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = fut

        try:
            await self._send(request)
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise TimeoutError(f"Cancel all request timed out: {req_id}")


# ----------------------------
# WebSocket Pool (Singleton)
# ----------------------------
class PacificaWSPool:
    """
    Singleton pool for Pacifica WebSocket connections.
    Shares connections across multiple exchange instances.
    """

    def __init__(self):
        self._clients: Dict[str, PacificaWSClient] = {}  # key: public_key
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        public_key: str,
        agent_public_key: Optional[str] = None,
        agent_keypair: Optional[Keypair] = None,
        subscribe_private: bool = True,
    ) -> PacificaWSClient:
        """
        Get or create a WebSocket client for the given account.

        Args:
            public_key: User's wallet address
            agent_public_key: Agent wallet address (for trading)
            agent_keypair: Agent keypair (for trading)
            subscribe_private: Auto-subscribe to private channels
        """
        key = public_key.lower()

        async with self._lock:
            if key in self._clients:
                client = self._clients[key]
                # Reconnect if needed
                if not client._running:
                    await client.connect()
                return client

            # Create new client
            client = PacificaWSClient(
                public_key=public_key,
                agent_public_key=agent_public_key,
                agent_keypair=agent_keypair,
            )
            await client.connect()

            # Subscribe to public prices by default
            await client.subscribe_prices()

            # Subscribe to private channels if requested
            if subscribe_private and public_key:
                await client.subscribe_all_private(public_key)

            self._clients[key] = client
            return client

    async def release(self, _public_key: str):
        """Release a client (does not close, keeps for reuse)"""
        pass

    async def close_all(self):
        """Close all connections"""
        async with self._lock:
            for client in self._clients.values():
                await client.close()
            self._clients.clear()


# Global singleton
PACIFICA_WS_POOL = PacificaWSPool()
