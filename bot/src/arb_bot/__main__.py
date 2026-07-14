from __future__ import annotations

import argparse
import base64
import atexit
import fcntl
import hashlib
import hmac
import html
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from .accounting import hedge_result_overfilled, parse_polymarket_buy_result, resolve_operation_final_pnl
from .core import Book, Level, ONE, Selection, ZERO, dec, parse_jsonish


LAST_DASHBOARD_PUBLISH_AT = 0.0
STATE_LOCK = threading.RLock()


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LiveTradeGuard:
    def __init__(self, lock_path: str = "work/bot.lock", stop_path: str = "work/STOP_BOT") -> None:
        self.lock_path = lock_path
        self.stop_path = stop_path
        self.fd: int | None = None

    def acquire(self) -> None:
        directory = os.path.dirname(self.lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.stop_path):
            raise RuntimeError(f"stop file exists: {self.stop_path}")
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another live bot is already running") from exc
        os.ftruncate(self.fd, 0)
        os.write(
            self.fd,
            json.dumps(
                {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()},
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        atexit.register(self.release)

    def check(self) -> None:
        if os.path.exists(self.stop_path):
            raise RuntimeError(f"stop file exists: {self.stop_path}")

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        finally:
            self.fd = None


def http_json(method: str, url: str, payload: Any | None = None, headers: dict[str, str] | None = None) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "previsao-polymarket-arb-scanner/0.1 (+https://previsao.io)",
        **(headers or {}),
    }
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc


def http_form(method: str, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "previsao-polymarket-arb-scanner/0.1 (+https://previsao.io)",
        **(headers or {}),
    }
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc


class PrevisaoClient:
    def __init__(self, base_url: str, api_key: str | None = None, api_secret: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret

    def auth_headers(self) -> dict[str, str]:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("missing PREVISAO_API_KEY/PREVISAO_API_SECRET")
        token = base64.b64encode(f"{self.api_key}:{self.api_secret}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Bearer {token}"}

    def mirrored_markets(self, page: int, limit: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"polymarketOnly": 1, "page": page, "limit": limit})
        return http_json("GET", f"{self.base_url}/markets?{query}")["data"]

    def markets(self, **params: Any) -> dict[str, Any]:
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        return http_json("GET", f"{self.base_url}/markets?{query}")["data"]

    def orderbook(self, market_id: int, limit: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"marketId": market_id, "limit": limit})
        return http_json("GET", f"{self.base_url}/orderbook?{query}")["data"]

    def me(self) -> Any:
        return http_json("GET", f"{self.base_url}/me", headers=self.auth_headers()).get("data")

    def balance(self) -> Any:
        return http_json("GET", f"{self.base_url}/balance", headers=self.auth_headers()).get("data")

    def orders(self, limit: int = 50, status: str = "OPEN") -> Any:
        query = urllib.parse.urlencode({"limit": limit, "status": status})
        return http_json("GET", f"{self.base_url}/orders?{query}", headers=self.auth_headers()).get("data")

    def trades(self, limit: int = 50) -> Any:
        query = urllib.parse.urlencode({"limit": limit})
        return http_json("GET", f"{self.base_url}/trades?{query}", headers=self.auth_headers()).get("data")

    def create_order(
        self,
        selection_id: str,
        side: str,
        order_type: str,
        price: Decimal | None = None,
        amount: Decimal | None = None,
        total: Decimal | None = None,
    ) -> Any:
        payload = {
            "selectionId": int(selection_id),
            "side": side,
            "type": order_type,
        }
        if price is not None:
            payload["price"] = str(price)
        if amount is not None:
            payload["amount"] = str(amount)
        if total is not None:
            payload["total"] = str(total)
        return http_json("POST", f"{self.base_url}/orders", payload=payload, headers=self.auth_headers()).get("data")

    def create_limit_buy(self, selection_id: str, price: Decimal, amount: Decimal) -> Any:
        return self.create_order(selection_id=selection_id, side="BUY", order_type="LIMIT", price=price, amount=amount)

    def create_market_sell(self, selection_id: str, amount: Decimal) -> Any:
        return self.create_order(selection_id=selection_id, side="SELL", order_type="MARKET", amount=amount)

    def cancel_order(self, order_id: str | int) -> Any:
        return http_json("DELETE", f"{self.base_url}/orders/{order_id}", headers=self.auth_headers()).get("data")


class PolymarketClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        address: str | None = None,
        private_key: str | None = None,
        funder: str | None = None,
        signature_type: int = 1,
        chain_id: int = 137,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.address = address
        self.private_key = private_key
        self.funder = funder
        self.signature_type = signature_type
        self.chain_id = chain_id
        self._signed_client: Any | None = None
        self._signed_api_creds: Any | None = None
        self.book_cache: PolymarketWsBookCache | None = None

    def books(self, token_ids: list[str]) -> dict[str, Book]:
        raw_books = self.book_cache.raw_books(token_ids) if self.book_cache else None
        if raw_books is None:
            raw_books = self.raw_books(token_ids)
        result: dict[str, Book] = {}
        for raw in raw_books:
            token_id = str(raw["asset_id"])
            result[token_id] = parse_poly_book(raw)
        return result

    def raw_books(self, token_ids: list[str]) -> list[dict[str, Any]]:
        payload = [{"token_id": token_id} for token_id in token_ids]
        return http_json("POST", f"{self.base_url}/books", payload)

    def auth_headers(self, method: str, path: str, body: str | None = None) -> dict[str, str]:
        if not self.api_key or not self.api_secret or not self.api_passphrase or not self.address:
            raise RuntimeError("missing POLYMARKET_API_KEY/POLYMARKET_API_SECRET/POLYMARKET_API_PASSPHRASE/POLYMARKET_ADDRESS")
        timestamp = str(self.server_time())
        signature = build_poly_hmac_signature(self.api_secret, timestamp, method, path, body)
        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
        }

    def user_orders(self) -> Any:
        path = "/data/orders"
        return http_json("GET", f"{self.base_url}{path}", headers=self.auth_headers("GET", path))

    def buy_fak(self, token_id: str, price: Decimal, shares: Decimal, spend_price: Decimal | None = None) -> Any:
        if not self.private_key:
            raise RuntimeError("missing POLYMARKET_PRIVATE_KEY for signed Polymarket orders")
        from py_clob_client_v2 import OrderArgs, OrderType

        price = quantize_price(price)
        shares = quantize_poly_buy_shares(shares, price)
        min_limit_shares = dec(os.getenv("POLYMARKET_MIN_ORDER_SHARES", "5"))
        if min_limit_shares > ZERO and shares < min_limit_shares:
            raise RuntimeError(f"Polymarket FAK too small: {shares} shares below {min_limit_shares}")
        client = self.signed_client()
        return client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(shares),
                side="BUY",
            ),
            order_type=OrderType.FAK,
        )

    def buy_market_usdc(self, token_id: str, amount_usdc: Decimal, max_price: Decimal) -> Any:
        if not self.private_key:
            raise RuntimeError("missing POLYMARKET_PRIVATE_KEY for signed Polymarket orders")
        from py_clob_client_v2 import MarketOrderArgs, OrderType

        amount_usdc = amount_usdc.quantize(Decimal("0.01"), rounding=ROUND_UP)
        if amount_usdc <= ZERO:
            raise RuntimeError(f"Polymarket market buy too small: {amount_usdc} USDC")
        max_price = quantize_price(max_price)
        client = self.signed_client()
        return client.create_and_post_market_order(
            MarketOrderArgs(
                token_id=token_id,
                amount=float(amount_usdc),
                side="BUY",
                price=float(max_price),
            ),
            order_type=OrderType.FAK,
        )

    def signed_client(self) -> Any:
        if self._signed_client is not None:
            return self._signed_client
        if not self.private_key:
            raise RuntimeError("missing POLYMARKET_PRIVATE_KEY for signed Polymarket orders")
        if not self.api_key or not self.api_secret or not self.api_passphrase:
            raise RuntimeError("missing Polymarket CLOB API credentials")
        from py_clob_client_v2 import ApiCreds, ClobClient

        client = ClobClient(
            self.base_url,
            chain_id=self.chain_id,
            key=self.private_key,
            signature_type=self.signature_type,
            funder=self.funder,
            retry_on_error=True,
        )
        client.set_api_creds(ApiCreds(self.api_key, self.api_secret, self.api_passphrase))
        self._signed_client = client
        return client

    def account_summary(self) -> dict[str, Any]:
        orders_raw = self.user_orders()
        orders = orders_raw.get("data", orders_raw) if isinstance(orders_raw, dict) else orders_raw
        summary: dict[str, Any] = {
            "connected": True,
            "address": self.address,
            "funder": self.funder,
            "open_orders": orders,
        }
        balance = self.collateral_balance()
        if balance is not None:
            summary["balance_usdc"] = balance
        return summary

    def collateral_balance(self) -> str | None:
        if not self.private_key:
            return None
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        except Exception:
            return None
        client = ClobClient(
            self.base_url,
            key=self.private_key,
            chain_id=self.chain_id,
            signature_type=self.signature_type,
            funder=self.funder,
        )
        if self.api_key and self.api_secret and self.api_passphrase:
            from py_clob_client.clob_types import ApiCreds

            client.set_api_creds(ApiCreds(self.api_key, self.api_secret, self.api_passphrase))
        raw = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self.signature_type)
        )
        raw_balance = dec(raw.get("balance", "0"))
        return str((raw_balance / Decimal("1000000")).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    def server_time(self) -> int:
        try:
            raw = http_json("GET", f"{self.base_url}/time")
            if isinstance(raw, dict):
                return int(raw.get("time") or raw.get("timestamp"))
            return int(raw)
        except Exception:
            return int(time.time())


class PolymarketGammaClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def markets(self, **params: Any) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        return http_json("GET", f"{self.base_url}/markets?{query}")


class PolymarketWsBookCache:
    def __init__(
        self,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        refresh_event: threading.Event | None = None,
    ) -> None:
        self.url = url
        self.refresh_event = refresh_event
        self._lock = threading.RLock()
        self._token_ids: set[str] = set()
        self._books: dict[str, dict[str, Any]] = {}
        self._updated_at: dict[str, float] = {}
        self._ws: Any | None = None
        self._started = False
        self.last_error: str | None = None
        self.last_message_at = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="polymarket-ws-books", daemon=True).start()

    def set_tokens(self, token_ids: list[str]) -> None:
        wanted = {str(token_id) for token_id in token_ids if token_id}
        with self._lock:
            if wanted == self._token_ids:
                return
            self._token_ids = wanted
            ws = self._ws
        if ws is not None and wanted:
            self._send_subscribe(ws, wanted)

    def raw_books(self, token_ids: list[str], max_age_seconds: float = 3.0) -> list[dict[str, Any]] | None:
        self.set_tokens(token_ids)
        now = time.time()
        with self._lock:
            rows = []
            for token_id in token_ids:
                token = str(token_id)
                raw = self._books.get(token)
                if raw is None or now - self._updated_at.get(token, 0.0) > max_age_seconds:
                    return None
                rows.append(json.loads(json.dumps(raw)))
            return rows

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "tokens": sorted(self._token_ids),
                "books": len(self._books),
                "last_message_age": None if not self.last_message_at else round(time.time() - self.last_message_at, 3),
                "last_error": self.last_error,
            }

    def _run(self) -> None:
        while True:
            try:
                import websocket

                ws = websocket.WebSocket()
                ws.connect(self.url, timeout=10)
                with self._lock:
                    self._ws = ws
                    tokens = set(self._token_ids)
                if tokens:
                    self._send_subscribe(ws, tokens)
                next_ping = time.time() + 10
                while True:
                    if time.time() >= next_ping:
                        ws.send("PING")
                        next_ping = time.time() + 10
                    ws.settimeout(1)
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        if "timed out" in str(exc).lower() or exc.__class__.__name__.lower().endswith("timeoutexception"):
                            continue
                        raise
                    if raw in ("PONG", b"PONG") or raw in ("PING", b"PING"):
                        continue
                    self.last_message_at = time.time()
                    self._handle_message(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
            except Exception as exc:
                self.last_error = str(exc)
                emit_warning("Polymarket WS disconnected; REST fallback stays active", error=str(exc))
                with self._lock:
                    self._ws = None
                time.sleep(2)

    def _send_subscribe(self, ws: Any, token_ids: set[str]) -> None:
        payload = {
            "type": "market",
            "assets_ids": sorted(token_ids),
            "initial_dump": True,
            "level": 2,
            "custom_feature_enabled": True,
        }
        try:
            ws.send(json.dumps(payload))
        except Exception as exc:
            self.last_error = str(exc)

    def _handle_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("asset_id") and ("bids" in row or "asks" in row):
                self._store_book(row)
                self._notify_refresh()
            for change in row.get("price_changes", []) if isinstance(row.get("price_changes"), list) else []:
                self._apply_price_change(change)
                self._notify_refresh()

    def _notify_refresh(self) -> None:
        if self.refresh_event is not None:
            self.refresh_event.set()

    def _store_book(self, row: dict[str, Any]) -> None:
        token_id = str(row.get("asset_id"))
        if not token_id:
            return
        raw_book = {
            "asset_id": token_id,
            "bids": list(row.get("bids") or []),
            "asks": list(row.get("asks") or []),
            "min_order_size": row.get("min_order_size"),
            "timestamp": row.get("timestamp"),
            "hash": row.get("hash"),
        }
        with self._lock:
            self._books[token_id] = raw_book
            self._updated_at[token_id] = time.time()

    def _apply_price_change(self, change: dict[str, Any]) -> None:
        token_id = str(change.get("asset_id", ""))
        if not token_id:
            return
        side = str(change.get("side", "")).upper()
        key = "bids" if side == "BUY" else "asks" if side == "SELL" else ""
        if not key:
            return
        price = str(change.get("price"))
        size = str(change.get("size", "0"))
        with self._lock:
            book = self._books.setdefault(token_id, {"asset_id": token_id, "bids": [], "asks": []})
            levels = [level for level in book.get(key, []) if str(level.get("price")) != price]
            if dec(size) > ZERO:
                levels.append({"price": price, "size": size})
            book[key] = levels
            book["timestamp"] = change.get("timestamp") or book.get("timestamp")
            self._updated_at[token_id] = time.time()


class PrevisaoWsHedgeAccelerator:
    def __init__(
        self,
        previsao: PrevisaoClient,
        poly: PolymarketClient,
        state_path: str,
        refresh_event: threading.Event | None = None,
    ) -> None:
        self.previsao = previsao
        self.poly = poly
        self.state_path = state_path
        self.refresh_event = refresh_event
        self._started = False
        self.last_error: str | None = None
        self.last_message_at = 0.0
        self.last_trade_event_at = 0.0
        self.hedge_triggers = 0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="previsao-ws-private", daemon=True).start()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": True,
            "last_message_age": None if not self.last_message_at else round(now - self.last_message_at, 3),
            "last_trade_event_age": None if not self.last_trade_event_at else round(now - self.last_trade_event_at, 3),
            "hedge_triggers": self.hedge_triggers,
            "last_error": self.last_error,
        }

    def _run(self) -> None:
        while True:
            try:
                import websocket

                config = http_json("GET", f"{self.previsao.base_url}/ws/config").get("data", {})
                broadcaster = config.get("broadcaster", {})
                key = broadcaster.get("key")
                host = broadcaster.get("host")
                port = broadcaster.get("port", 443)
                scheme = "wss" if broadcaster.get("useTLS", True) else "ws"
                if not key or not host:
                    raise RuntimeError("Previsao WS config missing broadcaster host/key")
                me = self.previsao.me() or {}
                user_id = me.get("userId") or me.get("id")
                if not user_id:
                    raise RuntimeError("Previsao /me did not return userId")

                url = f"{scheme}://{host}:{port}/app/{key}?protocol=7&client=python&version=1.0&flash=false"
                ws = websocket.WebSocket()
                ws.connect(url, timeout=10)
                next_ping = time.time() + 20
                while True:
                    if time.time() >= next_ping:
                        ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                        next_ping = time.time() + 20
                    ws.settimeout(1)
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        if "timed out" in str(exc).lower() or exc.__class__.__name__.lower().endswith("timeoutexception"):
                            continue
                        raise
                    self.last_message_at = time.time()
                    self._handle_message(ws, raw.decode("utf-8") if isinstance(raw, bytes) else str(raw), str(user_id))
            except Exception as exc:
                self.last_error = str(exc)
                emit_warning("Previsao private WS disconnected; polling fallback stays active", error=str(exc))
                time.sleep(2)

    def _handle_message(self, ws: Any, raw: str, user_id: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        event = str(message.get("event", ""))
        if event == "pusher:connection_established":
            data = parse_jsonish(message.get("data")) or {}
            socket_id = data.get("socket_id")
            if socket_id:
                channel = f"private-api.v1.user.{user_id}"
                auth = http_form(
                    "POST",
                    f"{self.previsao.base_url}/ws/auth",
                    {"socket_id": socket_id, "channel_name": channel},
                    headers=self.previsao.auth_headers(),
                )
                ws.send(json.dumps({"event": "pusher:subscribe", "data": {"channel": channel, "auth": auth.get("auth")}}))
            return
        if event == "trade.created":
            if self.refresh_event is not None:
                self.refresh_event.set()
            self.last_trade_event_at = time.time()
            self.hedge_triggers += 1
            try:
                payload = parse_jsonish(message.get("data")) or {}
                trade = payload.get("data", {}).get("trade") or payload.get("trade") or payload.get("data")
                trade_id = trade.get("id") if isinstance(trade, dict) else None
                with STATE_LOCK:
                    result = hedge_previsao_fills(self.previsao, self.poly, self.state_path)
                emit_warning("Previsao WS trade.created triggered hedge check", trade_id=trade_id, result=result)
            except Exception as exc:
                self.last_error = str(exc)
                emit_warning("Previsao WS hedge trigger failed", error=str(exc))


class PrevisaoWsPublicMonitor:
    def __init__(
        self,
        previsao: PrevisaoClient,
        refresh_event: threading.Event,
    ) -> None:
        self.previsao = previsao
        self.refresh_event = refresh_event
        self._started = False
        self.last_error: str | None = None
        self.last_message_at = 0.0
        self.last_refresh_event_at = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="previsao-ws-public", daemon=True).start()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": True,
            "last_message_age": None if not self.last_message_at else round(now - self.last_message_at, 3),
            "last_refresh_event_age": None if not self.last_refresh_event_at else round(now - self.last_refresh_event_at, 3),
            "last_error": self.last_error,
        }

    def _run(self) -> None:
        while True:
            try:
                import websocket

                config = http_json("GET", f"{self.previsao.base_url}/ws/config").get("data", {})
                broadcaster = config.get("broadcaster", {})
                key = broadcaster.get("key")
                host = broadcaster.get("host")
                port = broadcaster.get("port", 443)
                scheme = "wss" if broadcaster.get("useTLS", True) else "ws"
                if not key or not host:
                    raise RuntimeError("Previsao WS config missing broadcaster host/key")

                url = f"{scheme}://{host}:{port}/app/{key}?protocol=7&client=python&version=1.0&flash=false"
                ws = websocket.WebSocket()
                ws.connect(url, timeout=10)
                next_ping = time.time() + 20
                channels = ("api.v1.markets", "api.v1.prices.btcusd")
                while True:
                    if time.time() >= next_ping:
                        ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                        next_ping = time.time() + 20
                    ws.settimeout(1)
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        if "timed out" in str(exc).lower() or exc.__class__.__name__.lower().endswith("timeoutexception"):
                            continue
                        raise
                    self.last_message_at = time.time()
                    self._handle_message(ws, raw.decode("utf-8") if isinstance(raw, bytes) else str(raw), channels)
            except Exception as exc:
                self.last_error = str(exc)
                emit_warning("Previsao public WS disconnected; polling fallback stays active", error=str(exc))
                time.sleep(2)

    def _handle_message(self, ws: Any, raw: str, channels: tuple[str, ...]) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        event = str(message.get("event", ""))
        if event == "pusher:connection_established":
            for channel in channels:
                ws.send(json.dumps({"event": "pusher:subscribe", "data": {"channel": channel}}))
            return
        if event.startswith("pusher:"):
            return
        if event in {"book.delta", "reference_price.updated", "market.created", "market.updated", "market.settled"}:
            self.last_refresh_event_at = time.time()
            self.refresh_event.set()


class PolymarketWsUserMonitor:
    def __init__(
        self,
        poly: PolymarketClient,
        state_path: str,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user",
    ) -> None:
        self.poly = poly
        self.state_path = state_path
        self.url = url
        self._started = False
        self.last_error: str | None = None
        self.last_message_at = 0.0
        self.last_order_event_at = 0.0
        self.last_trade_event_at = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="polymarket-ws-user", daemon=True).start()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": True,
            "last_message_age": None if not self.last_message_at else round(now - self.last_message_at, 3),
            "last_order_event_age": None if not self.last_order_event_at else round(now - self.last_order_event_at, 3),
            "last_trade_event_age": None if not self.last_trade_event_at else round(now - self.last_trade_event_at, 3),
            "last_error": self.last_error,
        }

    def _run(self) -> None:
        if not self.poly.api_key or not self.poly.api_secret or not self.poly.api_passphrase:
            self.last_error = "missing Polymarket API credentials for user WS"
            return
        while True:
            try:
                import websocket

                ws = websocket.WebSocket()
                ws.connect(self.url, timeout=10)
                payload = {
                    "type": "user",
                    "auth": {
                        "apiKey": self.poly.api_key,
                        "secret": self.poly.api_secret,
                        "passphrase": self.poly.api_passphrase,
                    },
                }
                ws.send(json.dumps(payload))
                next_ping = time.time() + 10
                while True:
                    if time.time() >= next_ping:
                        ws.send("PING")
                        next_ping = time.time() + 10
                    ws.settimeout(1)
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        if "timed out" in str(exc).lower() or exc.__class__.__name__.lower().endswith("timeoutexception"):
                            continue
                        raise
                    if raw in ("PONG", b"PONG") or raw in ("PING", b"PING"):
                        continue
                    self.last_message_at = time.time()
                    self._handle_message(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
            except Exception as exc:
                self.last_error = str(exc)
                emit_warning("Polymarket user WS disconnected; REST/SDK fallback stays active", error=str(exc))
                time.sleep(2)

    def _handle_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            event_type = str(row.get("event_type") or row.get("type") or row.get("event") or "").lower()
            if event_type == "order":
                self.last_order_event_at = time.time()
                record_polymarket_user_event(self.state_path, row, "order")
                emit_warning(
                    "Polymarket user WS order event",
                    status=row.get("status") or row.get("type"),
                    order_id=row.get("id") or row.get("order_id"),
                    asset_id=row.get("asset_id"),
                    size_matched=row.get("size_matched"),
                )
            elif event_type == "trade":
                self.last_trade_event_at = time.time()
                record_polymarket_user_event(self.state_path, row, "trade")
                emit_warning(
                    "Polymarket user WS trade event",
                    status=row.get("status") or row.get("type"),
                    taker_order_id=row.get("taker_order_id"),
                    size=row.get("size"),
                    price=row.get("price"),
                )


def build_poly_hmac_signature(secret: str, timestamp: str, method: str, request_path: str, body: str | None = None) -> str:
    normalized = secret.replace("-", "+").replace("_", "/")
    normalized += "=" * ((4 - (len(normalized) % 4)) % 4)
    secret_bytes = base64.b64decode(normalized)
    message = f"{timestamp}{method}{request_path}"
    if body is not None:
        message += body
    digest = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii").replace("+", "-").replace("/", "_")


def best_level(levels: list[dict[str, Any]], side: str) -> Level | None:
    candidates: list[Level] = []
    for raw in levels:
        size = dec(raw.get("size", raw.get("amount", "0")))
        if size > ZERO:
            candidates.append(Level(price=dec(raw["price"]), size=size))
    if not candidates:
        return None
    if side == "bid":
        return max(candidates, key=lambda level: level.price)
    if side == "ask":
        return min(candidates, key=lambda level: level.price)
    raise ValueError(f"unknown book side: {side}")


def parse_poly_book(raw: dict[str, Any]) -> Book:
    min_order_size = dec(raw.get("min_order_size") or os.getenv("POLYMARKET_MIN_ORDER_SHARES", "0"))
    return Book(
        bid=best_level(raw.get("bids", []), "bid"),
        ask=best_level(raw.get("asks", []), "ask"),
        min_order_size=min_order_size,
    )


def estimate_poly_buy_usdc(raw_book: dict[str, Any], shares: Decimal, max_price: Decimal) -> Decimal:
    levels: list[Level] = []
    for raw in raw_book.get("asks", []):
        price = dec(raw.get("price", "0"))
        size = dec(raw.get("size", raw.get("amount", "0")))
        if ZERO < price <= max_price and size > ZERO:
            levels.append(Level(price=price, size=size))
    levels.sort(key=lambda level: level.price)

    remaining = shares
    cost = ZERO
    for level in levels:
        if remaining <= ZERO:
            break
        take = min(remaining, level.size)
        cost += take * level.price
        remaining -= take
    if remaining > ZERO:
        cost += remaining * max_price

    buffer_pct = dec(os.getenv("POLYMARKET_HEDGE_USDC_BUFFER_PCT", "1")) / Decimal("100")
    return (cost * (ONE + buffer_pct) + Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_UP)


def parse_previsao_book(raw: dict[str, Any], selection_id: str) -> Book:
    selection_book = raw.get("books", {}).get(selection_id, {})
    return Book(
        bid=best_level(selection_book.get("bids", []), "bid"),
        ask=best_level(selection_book.get("asks", []), "ask"),
    )


def max_shares_for_lock(a: Level, b: Level) -> Decimal:
    return min(a.size, b.size)


def max_shares_for_direct(buy_ask: Level, sell_bid: Level) -> Decimal:
    return min(buy_ask.size, sell_bid.size)


def edge_bps(edge: Decimal) -> Decimal:
    return edge * Decimal("10000")


def emit_opportunity(kind: str, market: dict[str, Any], edge: Decimal, shares: Decimal, details: dict[str, Any]) -> None:
    row = {
        "kind": kind,
        "edge_bps": str(edge_bps(edge).quantize(Decimal("0.01"))),
        "max_shares_visible": str(shares),
        "market_id": market["id"],
        "title": market["title"],
        "previsao_url": f"https://previsao.io/pt/market/{market['slug']}",
        "polymarket_url": market.get("polymarket", {}).get("marketUrl"),
        "details": details,
    }
    print(json.dumps(row, ensure_ascii=False))


def emit_warning(message: str, **extra: Any) -> None:
    print(json.dumps({"level": "warning", "message": message, **extra}, ensure_ascii=False), file=sys.stderr)


def emit_account_snapshot(previsao: PrevisaoClient, poly: PolymarketClient, raw: bool = False) -> None:
    snapshot: dict[str, Any] = {"previsao": {}, "polymarket": {}}

    try:
        me = previsao.me()
        balance = previsao.balance()
        open_orders = previsao.orders(limit=50, status="OPEN")
        recent_trades = previsao.trades(limit=25)
        if raw:
            snapshot["previsao"]["me"] = me
            snapshot["previsao"]["balance"] = balance
            snapshot["previsao"]["open_orders"] = open_orders
            snapshot["previsao"]["recent_trades"] = recent_trades
        else:
            snapshot["previsao"] = {
                "authenticated": bool(me),
                "userId": me.get("userId") if isinstance(me, dict) else None,
                "balance": balance,
                "open_orders_count": len(open_orders or []),
                "recent_trades_count": len(recent_trades or []),
            }
    except Exception as exc:
        snapshot["previsao"]["error"] = str(exc)

    if poly.api_key and poly.api_secret and poly.api_passphrase and poly.address:
        try:
            snapshot["polymarket"]["open_orders"] = poly.user_orders()
        except Exception as exc:
            snapshot["polymarket"]["error"] = str(exc)
    else:
        snapshot["polymarket"]["skipped"] = "set POLYMARKET_ADDRESS in addition to L2 credentials for authenticated CLOB reads"

    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


def scan_market(market: dict[str, Any], previsao: PrevisaoClient, poly: PolymarketClient, min_edge: Decimal, depth: int) -> None:
    selections = market.get("selections", [])
    if len(selections) < 2:
        return

    pre_book_raw = previsao.orderbook(int(market["id"]), depth)
    token_ids = [str(s["clobTokenId"]) for s in selections if s.get("clobTokenId")]
    poly_books = poly.books(token_ids)

    parsed: list[tuple[Selection, Selection]] = []
    for raw in selections:
        selection_id = str(raw["id"])
        token_id = str(raw["clobTokenId"])
        label = str(raw.get("label") or raw.get("code") or selection_id)
        parsed.append(
            (
                Selection("previsao", str(market["id"]), selection_id, token_id, label, parse_previsao_book(pre_book_raw, selection_id)),
                Selection("polymarket", str(market.get("polymarket", {}).get("marketId")), selection_id, token_id, label, poly_books.get(token_id, Book(None, None))),
            )
        )

    for pre, pm in parsed:
        check_direct(market, pre, pm, min_edge)
        check_direct(market, pm, pre, min_edge)

    # Binary mirror lock: buy one outcome in one venue and the complement in the other.
    if len(parsed) == 2:
        pre_a, pm_a = parsed[0]
        pre_b, pm_b = parsed[1]
        check_lock(market, "LOCK_YES_PREV_NO_POLY", pre_a, pm_b, min_edge)
        check_lock(market, "LOCK_NO_PREV_YES_POLY", pre_b, pm_a, min_edge)


def scan_bitcoin_5m(
    previsao: PrevisaoClient,
    gamma: PolymarketGammaClient,
    poly: PolymarketClient,
    min_edge: Decimal,
    depth: int,
    debug_books: bool = False,
    maker_plan: bool = False,
    maker_margin_pct: Decimal = Decimal("15"),
    max_order_usdc: Decimal = Decimal("2"),
    min_seconds_left: int = 90,
    execute_previsao_maker: bool = False,
) -> int:
    data = previsao.markets(
        search="Bitcoin",
        status="OPEN",
        orderBy="closesAt",
        orderDirection="ASC",
        limit=10,
    )
    pre_markets = [
        market
        for market in data.get("items", [])
        if "5 minutes" in str(market.get("title", "")).lower()
        and "up or down" in str(market.get("title", "")).lower()
        and not is_expired(market)
    ]
    if not pre_markets:
        emit_warning("no active Previsao Bitcoin 5m market found")
        return 0

    scanned = 0
    for pre_market in pre_markets:
        poly_market = find_matching_poly_btc_5m(gamma, pre_market)
        if poly_market is None:
            emit_warning(
                "no matching Polymarket BTC 5m market found",
                previsao_market_id=pre_market.get("id"),
                opensAt=pre_market.get("opensAt"),
                closesAt=pre_market.get("closesAt"),
            )
            continue
        scan_bitcoin_5m_pair(
            previsao,
            poly,
            pre_market,
            poly_market,
            min_edge,
            depth,
            debug_books=debug_books,
            maker_plan=maker_plan,
            maker_margin_pct=maker_margin_pct,
            max_order_usdc=max_order_usdc,
            min_seconds_left=min_seconds_left,
            execute_previsao_maker=execute_previsao_maker,
        )
        scanned += 1
    return scanned


def get_active_previsao_btc_5m(previsao: PrevisaoClient) -> list[dict[str, Any]]:
    data = previsao.markets(
        search="Bitcoin",
        status="OPEN",
        orderBy="closesAt",
        orderDirection="ASC",
        limit=10,
    )
    return [
        market
        for market in data.get("items", [])
        if "5 minutes" in str(market.get("title", "")).lower()
        and "up or down" in str(market.get("title", "")).lower()
        and not is_expired(market)
    ]


def build_bitcoin_5m_snapshot(
    previsao: PrevisaoClient,
    gamma: PolymarketGammaClient,
    poly: PolymarketClient,
    maker_margin_pct: Decimal,
    max_order_usdc: Decimal,
    min_seconds_left: int,
    execute: bool = False,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "strategy": "previsao_maker_polymarket_reference",
        "maker_margin_pct": str(maker_margin_pct),
        "max_order_usdc": str(max_order_usdc),
        "min_seconds_left": min_seconds_left,
        "execute": execute,
        "market": None,
        "account": {},
        "warnings": ["Polymarket and Previsao do not settle atomically; fills can remain unhedged."],
    }
    try:
        snapshot["account"]["balance"] = previsao.balance()
        snapshot["account"]["open_orders"] = previsao.orders(limit=100, status="OPEN")
        state_path = os.getenv("BOT_STATE_PATH", "work/hedge_state.json")
        state = enrich_hedged_trade_settlements(previsao, state_path)
        snapshot["account"]["operations"] = summarize_hedged_operations_from_state(state)
    except Exception as exc:
        snapshot["account"]["error"] = str(exc)
    if poly.api_key and poly.api_secret and poly.api_passphrase and poly.address:
        try:
            snapshot["account"]["polymarket"] = poly.account_summary()
        except Exception as exc:
            snapshot["account"]["polymarket"] = {
                "connected": False,
                "address": poly.address,
                "funder": poly.funder,
                "error": str(exc),
            }
    else:
        snapshot["account"]["polymarket"] = {
            "connected": False,
            "address": poly.address,
            "error": "Polymarket ainda não está ligada para ler conta.",
        }

    pre_markets = get_active_previsao_btc_5m(previsao)
    if not pre_markets:
        snapshot["warnings"].append("No active Previsao Bitcoin 5m market found.")
        return snapshot

    pre_market = pre_markets[0]
    poly_market = find_matching_poly_btc_5m(gamma, pre_market)
    if poly_market is None:
        snapshot["market"] = {"previsao": summarize_previsao_market(pre_market), "polymarket": None}
        snapshot["warnings"].append("No matching Polymarket BTC 5m market found.")
        return snapshot

    pre_book_raw = previsao.orderbook(int(pre_market["id"]), 50)
    poly_token_ids = parse_jsonish_list(poly_market.get("clobTokenIds"))
    poly_outcomes = parse_jsonish_list(poly_market.get("outcomes"))
    poly_books = poly.books([str(token_id) for token_id in poly_token_ids])
    pre_by_label = build_previsao_selections(pre_market, pre_book_raw)
    poly_by_label = build_poly_selections(poly_market, poly_outcomes, poly_token_ids, poly_books)
    plans = build_maker_quote_plans(pre_by_label, poly_by_label, maker_margin_pct, max_order_usdc)

    closes_at = parse_datetime(pre_market.get("closesAt"))
    seconds_left = None if closes_at is None else int((closes_at - datetime.now(timezone.utc)).total_seconds())
    can_quote = seconds_left is not None and seconds_left > min_seconds_left
    sync_result = None
    hedge_result = None
    state_path = os.getenv("BOT_STATE_PATH", "work/hedge_state.json")
    if execute:
        hedge_result = hedge_previsao_fills(previsao, poly, state_path)
    if execute and can_quote:
        sync_result = sync_previsao_maker_orders(previsao, int(pre_market["id"]), plans, state_path)
    elif execute and not can_quote:
        sync_result = cancel_all_previsao_maker_orders(previsao)
        sync_result["skipped"] = "too close to close"
        sync_result["seconds_left"] = seconds_left

    snapshot["market"] = {
        "previsao": summarize_previsao_market(pre_market),
        "polymarket": summarize_poly_market(poly_market),
        "seconds_left": seconds_left,
        "can_quote": can_quote,
        "books": {
            "previsao": {label: format_book(selection.book) for label, selection in pre_by_label.items()},
            "polymarket": {label: format_book(selection.book) for label, selection in poly_by_label.items()},
        },
        "quotes": plans,
        "hedge": hedge_result,
        "sync": sync_result,
    }
    return snapshot


def build_previsao_selections(pre_market: dict[str, Any], pre_book_raw: dict[str, Any]) -> dict[str, Selection]:
    result: dict[str, Selection] = {}
    for raw in pre_market.get("selections", []):
        label = normalize_outcome_label(raw.get("label"))
        selection_id = str(raw["id"])
        result[label] = Selection(
            "previsao",
            str(pre_market["id"]),
            selection_id,
            str(raw.get("customId", "")),
            label.title(),
            parse_previsao_book(pre_book_raw, selection_id),
        )
    return result


def build_poly_selections(
    poly_market: dict[str, Any],
    poly_outcomes: list[Any],
    poly_token_ids: list[Any],
    poly_books: dict[str, Book],
) -> dict[str, Selection]:
    result: dict[str, Selection] = {}
    for index, raw_label in enumerate(poly_outcomes):
        label = normalize_outcome_label(raw_label)
        token_id = str(poly_token_ids[index])
        result[label] = Selection(
            "polymarket",
            str(poly_market.get("id")),
            str(poly_market.get("id")),
            token_id,
            label.title(),
            poly_books.get(token_id, Book(None, None)),
        )
    return result


def build_maker_quote_plans(
    pre_by_label: dict[str, Selection],
    poly_by_label: dict[str, Selection],
    maker_margin_pct: Decimal,
    max_order_usdc: Decimal,
) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    margin = maker_margin_pct / Decimal("100")
    maybe_add_maker_quote(
        plans,
        outcome="up",
        hedge_outcome="down",
        pre_selection=pre_by_label.get("up"),
        reference_selection=poly_by_label.get("up"),
        hedge_selection=poly_by_label.get("down"),
        margin=margin,
        max_order_usdc=max_order_usdc,
    )
    maybe_add_maker_quote(
        plans,
        outcome="down",
        hedge_outcome="up",
        pre_selection=pre_by_label.get("down"),
        reference_selection=poly_by_label.get("down"),
        hedge_selection=poly_by_label.get("up"),
        margin=margin,
        max_order_usdc=max_order_usdc,
    )
    return choose_maker_plans(plans)


def choose_maker_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if os.getenv("BOT_QUOTE_BOTH_SIDES", "0") == "1":
        return plans
    quote_indexes = [index for index, plan in enumerate(plans) if plan.get("status") == "quote"]
    if len(quote_indexes) <= 1:
        return plans
    best_index = max(
        quote_indexes,
        key=lambda index: (
            dec(plans[index].get("gross_edge_if_hedged_now", "0")),
            dec(plans[index].get("previsao_order", {}).get("max_cost", "0")),
        ),
    )
    selected: list[dict[str, Any]] = []
    for index, plan in enumerate(plans):
        if index == best_index or plan.get("status") != "quote":
            selected.append(plan)
            continue
        selected.append(
            {
                **plan,
                "status": "skipped",
                "reason": "only one quote per round; another side has better edge",
                "would_quote": plan.get("previsao_order"),
            }
        )
    return selected


def summarize_previsao_market(market: dict[str, Any]) -> dict[str, Any]:
    current_price = (
        market.get("currentPrice")
        or market.get("current_price")
        or market.get("closingPrice")
        or market.get("closePrice")
        or market.get("lastPrice")
    )
    return {
        "id": market.get("id"),
        "slug": market.get("slug"),
        "title": market.get("title"),
        "opensAt": market.get("opensAt"),
        "closesAt": market.get("closesAt"),
        "initialPrice": market.get("initialPrice"),
        "currentPrice": current_price,
        "resultSource": market.get("resultSource"),
        "url": f"https://previsao.io/pt/market/{market.get('slug')}",
    }


def summarize_poly_market(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": market.get("id"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "endDate": market.get("endDate"),
        "acceptingOrders": market.get("acceptingOrders"),
        "url": f"https://polymarket.com/event/{market.get('slug')}",
    }


DASHBOARD_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bot BTC 5 min</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0d10;
      --panel: #151922;
      --panel-2: #1d2330;
      --line: #2a3342;
      --text: #edf2f7;
      --muted: #9aa6b2;
      --green: #25c26e;
      --red: #ff5f5f;
      --yellow: #e4b84f;
      --blue: #65a9ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 5;
      background: rgba(11, 13, 16, 0.94);
      border-bottom: 1px solid var(--line);
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      max-width: 1280px;
      margin: 0 auto;
      padding: 14px 18px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .subtle { color: var(--muted); }
    .controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      white-space: nowrap;
    }
    input {
      width: 78px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 7px 8px;
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { border-color: var(--blue); }
    button.danger { border-color: rgba(255,95,95,.45); color: #ffd0d0; }
    main {
      max-width: 1280px;
      margin: 0 auto;
      padding: 14px 18px;
      display: grid;
      gap: 10px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 10px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px 12px;
      min-width: 0;
    }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-5 { grid-column: span 5; }
    .span-6 { grid-column: span 6; }
    .span-7 { grid-column: span 7; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .kicker {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 5px;
    }
    .value {
      font-size: 24px;
      line-height: 1.1;
      font-weight: 750;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .small { font-size: 12px; color: var(--muted); }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 7px 6px;
      vertical-align: middle;
      overflow-wrap: anywhere;
    }
    th { color: var(--muted); font-weight: 600; font-size: 12px; }
    tr:last-child td { border-bottom: 0; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 2px 8px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--muted);
      font-size: 12px;
    }
    .ok { color: var(--green); }
    .bad { color: var(--red); }
    .warn { color: var(--yellow); }
    .quote {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 10px;
      margin-top: 8px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
      padding: 8px;
      min-width: 0;
    }
    .metric b { display: block; font-size: 18px; margin-top: 4px; overflow-wrap: anywhere; }
    .market-line {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
    }
    .market-line b { font-size: 16px; }
    .quote-row {
      display: grid;
      grid-template-columns: 80px repeat(4, minmax(0, 1fr));
      gap: 8px;
      align-items: center;
      border-top: 1px solid var(--line);
      padding: 9px 0;
    }
    .quote-row:first-child { border-top: 0; }
    .compact-value { font-size: 20px; font-weight: 750; line-height: 1.15; }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    @media (max-width: 900px) {
      .span-3, .span-4, .span-5, .span-6, .span-7, .span-8 { grid-column: span 12; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .controls { justify-content: flex-start; }
      .quote { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Bot BTC 5 min</h1>
        <div class="small" id="status">Conectando...</div>
      </div>
      <div class="controls">
        <label>Desconto % <input id="margin" type="number" min="0" max="90" step="0.5" value="15"></label>
        <label>Máx por aposta <input id="maxOrder" type="number" min="0.1" step="0.1" value="2"></label>
        <label>Parar faltando <input id="minSeconds" type="number" min="0" step="5" value="10"></label>
      </div>
    </div>
  </header>
  <main>
    <section class="panel">
      <div class="market-line">
        <div>
          <div class="kicker">Mercado</div>
          <b id="marketTitle">Bitcoin 5 min</b>
          <div class="small" id="marketLinks">--</div>
        </div>
        <div class="small" id="streamState">Ligado em tempo real</div>
      </div>
    </section>

    <section class="grid">
      <div class="panel span-4">
        <div class="kicker">Preço inicial</div>
        <div class="value" id="initialPrice">--</div>
      </div>
      <div class="panel span-4">
        <div class="kicker">Preço atual</div>
        <div class="value" id="currentPrice">--</div>
        <div class="small">quando a Previsao enviar</div>
      </div>
      <div class="panel span-4">
        <div class="kicker">Min / seg</div>
        <div class="value" id="secondsLeft">--</div>
        <div class="small" id="quoteState">--</div>
      </div>
    </section>

    <section class="grid">
      <div class="panel span-3">
        <div class="kicker">Saldo Previsao</div>
        <div class="value" id="balance">--</div>
        <div class="small">USDC livre</div>
      </div>
      <div class="panel span-3">
        <div class="kicker">Saldo Polymarket</div>
        <div class="value" id="polymarketSide">--</div>
        <div class="small" id="polymarketSideText">--</div>
      </div>
      <div class="panel span-6">
        <div class="kicker">Ordens</div>
        <div class="value" id="openOrdersCount">--</div>
        <div class="small">abertas na Previsao</div>
      </div>
    </section>

    <section class="grid">
      <div class="panel span-6">
        <div class="kicker">Compra/venda Previsao</div>
        <table>
          <thead><tr><th>Lado</th><th>Compram</th><th>Vendem</th></tr></thead>
          <tbody id="previsaoBooks"></tbody>
        </table>
      </div>
      <div class="panel span-6">
        <div class="kicker">Compra/venda Polymarket</div>
        <table>
          <thead><tr><th>Lado</th><th>Compram</th><th>Vendem</th></tr></thead>
          <tbody id="polyBooks"></tbody>
        </table>
      </div>
    </section>

    <section class="panel">
        <div class="kicker">Ordens abertas</div>
        <table>
          <thead><tr><th>ID</th><th>Lado</th><th>Preço</th><th>Falta</th></tr></thead>
          <tbody id="orders"></tbody>
        </table>
    </section>

    <section class="panel">
      <div class="kicker">Lucro/prejuízo</div>
      <div class="value" id="operationsTotal">--</div>
      <div class="small" id="operationsNote">cada linha junta Previsao + Polymarket</div>
      <table>
        <thead><tr><th>Hora</th><th>Previsao</th><th>Polymarket</th><th>Resultado</th></tr></thead>
        <tbody id="operations"></tbody>
      </table>
    </section>

  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const fmt = (v) => v === null || v === undefined || v === "" ? "--" : String(v);
    const money = (v) => v === null || v === undefined ? "--" : Number(v).toFixed(2);
    function params() {
      return new URLSearchParams({
        margin: $("margin").value || "15",
        max_order: $("maxOrder").value || "2",
        min_seconds: $("minSeconds").value || "10"
      });
    }
    function levelText(level) {
      if (!level) return "--";
      return `${level.price} / ${level.size}`;
    }
    function mmss(seconds) {
      if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "--";
      const value = Math.max(0, Math.floor(seconds));
      const m = String(Math.floor(value / 60)).padStart(2, "0");
      const s = String(value % 60).padStart(2, "0");
      return `${m}:${s}`;
    }
    async function loadSnapshot() {
      try {
        const res = await fetch(`/api/snapshot?${params()}`);
        const data = await res.json();
        render(data);
        $("status").textContent = "Ligado";
      } catch (err) {
        $("status").textContent = `Erro: ${err.message}`;
        console.error(err);
      }
    }
    let latestSnapshot = null;
    let stream = null;
    function startStream() {
      if (stream) stream.close();
      stream = new EventSource(`/api/stream?${params()}&interval=1`);
      $("streamState").textContent = "Ligado em tempo real";
      stream.onmessage = (event) => {
        latestSnapshot = JSON.parse(event.data);
        render(latestSnapshot);
        $("status").textContent = "Ligado";
        $("streamState").textContent = "Ligado em tempo real";
      };
      stream.onerror = () => {
        $("status").textContent = "Reconectando...";
        $("streamState").textContent = "Tentando ligar de novo";
      };
    }
    function render(data) {
      const market = data.market || {};
      const account = data.account || {};
      const balance = (account.balance || []).find((row) => row.currency === "USDC");
      const polyAccount = account.polymarket || {};
      $("secondsLeft").textContent = mmss(market.seconds_left);
      $("quoteState").innerHTML = market.can_quote ? '<span class="ok">Bot pode colocar ordem</span>' : '<span class="warn">Bot parou esta rodada</span>';
      $("balance").textContent = balance ? money(balance.amount) : "--";
      $("openOrdersCount").textContent = (account.open_orders || []).length;
      $("polymarketSide").textContent = polyAccount.connected
        ? `${fmt(polyAccount.balance_usdc)} USDC`
        : "Não conectado";
      $("polymarketSideText").textContent = polyAccount.connected
        ? `Ordens abertas: ${(polyAccount.open_orders || []).length}`
        : "Ainda só estamos usando os preços públicos da Polymarket";

      const pre = market.previsao || {};
      const poly = market.polymarket || {};
      $("marketTitle").textContent = pre.title || poly.question || "Bitcoin 5 min";
      $("marketLinks").innerHTML = [
        pre.url ? `<a href="${pre.url}" target="_blank">Abrir Previsao</a>` : "",
        poly.url ? `<a href="${poly.url}" target="_blank">Abrir Polymarket</a>` : ""
      ].filter(Boolean).join(" · ") || "--";
      $("initialPrice").textContent = fmt(pre.initialPrice);
      $("currentPrice").textContent = fmt(pre.currentPrice);
      renderBooks("previsaoBooks", market.books?.previsao || {});
      renderBooks("polyBooks", market.books?.polymarket || {});
      renderOrders(account.open_orders || []);
      renderOperations(account.operations || []);
    }
    function renderBooks(id, books) {
      $(id).innerHTML = ["up", "down"].map((outcome) => {
        const book = books[outcome] || {};
        const name = outcome === "up" ? "SOBE" : "DESCE";
        return `<tr><td><span class="pill">${name}</span><div class="small">preço do meio ${fmt(book.mid)} · distância ${fmt(book.spread)}</div></td><td>${levelText(book.bid)}</td><td>${levelText(book.ask)}</td></tr>`;
      }).join("");
    }
    function simpleReason(reason) {
      if (reason === "hedge ask leaves no positive quote after margin") return "A Polymarket está cara demais agora, então o bot não coloca ordem.";
      if (reason === "missing hedge ask") return "Não tem preço claro na Polymarket para copiar.";
      if (reason === "zero hedgeable shares") return "Não tem quantidade suficiente para copiar.";
      if (reason === "previsao minimum order not met") return "A quantidade disponível agora é pequena demais para abrir uma ordem.";
      if (reason === "polymarket minimum hedge not met") return "A Polymarket não deixa proteger tão pouco desse lado agora.";
      if (reason === "quote price too small") return "O preço ficou baixo demais; o bot espera uma chance mais limpa.";
      return fmt(reason);
    }
    function renderOrders(orders) {
      $("orders").innerHTML = orders.length ? orders.map((o) => (
        `<tr><td>${fmt(o.id)}</td><td>${fmt(o.side)} ${fmt(o.selectionId)}</td><td>${fmt(o.price)}</td><td>${fmt(o.amountRemaining ?? o.amount)}</td></tr>`
      )).join("") : '<tr><td colspan="4" class="subtle">Nenhuma ordem aberta.</td></tr>';
    }
    function renderOperations(operations) {
      const pnl = (op) => Number(op.gross_profit_worst ?? op.gross_profit_expected ?? 0);
      const total = operations.reduce((sum, op) => sum + pnl(op), 0);
      $("operationsTotal").innerHTML = operations.length
        ? `<span class="${total >= 0 ? "ok" : "bad"}">${total >= 0 ? "+" : ""}${total.toFixed(2)} USDC</span>`
        : "--";
      $("operationsNote").textContent = operations.length
        ? `${operations.length} operações protegidas`
        : "cada linha junta Previsao + Polymarket";
      $("operations").innerHTML = operations.length ? operations.map((op) => {
        const value = pnl(op);
        const resultClass = value >= 0 ? "ok" : "bad";
        const preCost = op.previsao_cost ? `${op.previsao_cost} USDC` : "--";
        const polyCost = op.polymarket_cost_real || op.polymarket_cost_max;
        const extra = Number(op.extra_hedge_shares || 0);
        return `<tr>
          <td>${fmt(op.hedged_at).slice(11, 19)}</td>
          <td><b>${fmt(op.previsao)} @ ${fmt(op.previsao_price)}</b><div class="small">${fmt(op.shares)} qtd · pagou ${preCost}</div></td>
          <td><b>${fmt(op.polymarket)} até ${fmt(op.polymarket_max_price)}</b><div class="small">pagou ${fmt(polyCost)} USDC${extra > 0 ? ` · sobrou ${extra.toFixed(4)} qtd` : ""}</div></td>
          <td><b class="${resultClass}">${value >= 0 ? "+" : ""}${value.toFixed(2)} USDC</b></td>
        </tr>`;
      }).join("") : '<tr><td colspan="4" class="subtle">Nenhuma operação protegida ainda.</td></tr>';
    }
    ["margin", "maxOrder", "minSeconds"].forEach((id) => $(id).addEventListener("change", startStream));
    startStream();
    setInterval(() => {
      if (!latestSnapshot?.market?.previsao?.closesAt) return;
      const closeMs = Date.parse(latestSnapshot.market.previsao.closesAt);
      $("secondsLeft").textContent = mmss((closeMs - Date.now()) / 1000);
    }, 250);
  </script>
</body>
</html>
"""


def run_dashboard(
    previsao: PrevisaoClient,
    gamma: PolymarketGammaClient,
    poly: PolymarketClient,
    host: str,
    port: int,
) -> None:
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send_html(DASHBOARD_HTML)
                return
            if parsed.path == "/api/stream":
                self.send_stream(parsed.query)
                return
            if parsed.path == "/api/snapshot":
                self.send_json(self.snapshot_from_query(parsed.query, execute=False))
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/sync":
                params = urllib.parse.parse_qs(parsed.query)
                execute = params.get("confirm", [""])[0] == "EXECUTE"
                guard = LiveTradeGuard(
                    os.getenv("BOT_LOCK_PATH", "work/bot.lock"),
                    os.getenv("BOT_STOP_PATH", "work/STOP_BOT"),
                )
                try:
                    if execute:
                        guard.acquire()
                    self.send_json(self.snapshot_from_query(parsed.query, execute=execute))
                except Exception as exc:
                    self.send_json(
                        {
                            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "error": str(exc),
                            "market": None,
                            "account": {},
                            "warnings": ["A trava de segurança bloqueou a ação ao vivo."],
                        }
                    )
                finally:
                    guard.release()
                return
            self.send_error(404)

        def snapshot_from_query(self, query: str, execute: bool) -> dict[str, Any]:
            params = urllib.parse.parse_qs(query)
            margin = dec(params.get("margin", ["15"])[0])
            max_order = dec(params.get("max_order", ["2"])[0])
            min_seconds = int(params.get("min_seconds", ["90"])[0])
            try:
                return build_bitcoin_5m_snapshot(
                    previsao,
                    gamma,
                    poly,
                    maker_margin_pct=margin,
                    max_order_usdc=max_order,
                    min_seconds_left=min_seconds,
                    execute=execute,
                )
            except Exception as exc:
                return {
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "error": str(exc),
                    "market": None,
                    "account": {},
                    "warnings": ["Snapshot failed."],
                }

        def send_html(self, content: str) -> None:
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_stream(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            interval = max(0.5, float(params.get("interval", ["1"])[0]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                payload = self.snapshot_from_query(query, execute=False)
                frame = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(interval)

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}", file=sys.stderr)
    server.serve_forever()


def find_matching_poly_btc_5m(gamma: PolymarketGammaClient, pre_market: dict[str, Any]) -> dict[str, Any] | None:
    opens_at = parse_datetime(pre_market.get("opensAt"))
    closes_at = parse_datetime(pre_market.get("closesAt"))
    if opens_at is None or closes_at is None:
        return None
    for offset in range(0, 600, 100):
        candidates = gamma.markets(
            limit=100,
            offset=offset,
            closed="false",
            end_date_min=(closes_at - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            end_date_max=(closes_at + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            order="endDate",
            ascending="true",
        )
        if not candidates:
            break
        for market in candidates:
            slug = str(market.get("slug", "")).lower()
            question = str(market.get("question", "")).lower()
            end_date = parse_datetime(market.get("endDate"))
            if end_date is not None and end_date > closes_at + timedelta(seconds=1):
                return None
            if end_date != closes_at:
                continue
            if slug.startswith("btc-updown-5m-") or ("bitcoin up or down" in question and slug.startswith("btc-updown-5m-")):
                return market
        if len(candidates) < 100:
            break
    return None


def scan_bitcoin_5m_pair(
    previsao: PrevisaoClient,
    poly: PolymarketClient,
    pre_market: dict[str, Any],
    poly_market: dict[str, Any],
    min_edge: Decimal,
    depth: int,
    debug_books: bool = False,
    maker_plan: bool = False,
    maker_margin_pct: Decimal = Decimal("15"),
    max_order_usdc: Decimal = Decimal("2"),
    min_seconds_left: int = 90,
    execute_previsao_maker: bool = False,
) -> None:
    pre_book_raw = previsao.orderbook(int(pre_market["id"]), depth)
    poly_token_ids = parse_jsonish_list(poly_market.get("clobTokenIds"))
    poly_outcomes = parse_jsonish_list(poly_market.get("outcomes"))
    poly_books = poly.books([str(token_id) for token_id in poly_token_ids])

    pre_by_label: dict[str, Selection] = {}
    for raw in pre_market.get("selections", []):
        label = normalize_outcome_label(raw.get("label"))
        selection_id = str(raw["id"])
        pre_by_label[label] = Selection(
            "previsao",
            str(pre_market["id"]),
            selection_id,
            str(raw.get("customId", "")),
            label.title(),
            parse_previsao_book(pre_book_raw, selection_id),
        )

    poly_by_label: dict[str, Selection] = {}
    for index, raw_label in enumerate(poly_outcomes):
        label = normalize_outcome_label(raw_label)
        token_id = str(poly_token_ids[index])
        poly_by_label[label] = Selection(
            "polymarket",
            str(poly_market.get("id")),
            str(poly_market.get("id")),
            token_id,
            label.title(),
            poly_books.get(token_id, Book(None, None)),
        )

    if debug_books:
        print(
            json.dumps(
                {
                    "level": "debug",
                    "market": "bitcoin_5m",
                    "previsao_market_id": pre_market.get("id"),
                    "polymarket_market_id": poly_market.get("id"),
                    "books": {
                        "previsao": {label: format_book(selection.book) for label, selection in pre_by_label.items()},
                        "polymarket": {label: format_book(selection.book) for label, selection in poly_by_label.items()},
                    },
                },
                ensure_ascii=False,
            )
        )

    market_context = {
        "id": pre_market["id"],
        "slug": pre_market["slug"],
        "title": pre_market["title"],
        "polymarket": {"marketUrl": f"https://polymarket.com/event/{poly_market.get('slug')}"},
    }
    emit_warning(
        "btc 5m reference sources may differ; treat as basis trade, not pure arbitrage",
        previsao_source=pre_market.get("resultSource"),
        polymarket_market_id=poly_market.get("id"),
        polymarket_slug=poly_market.get("slug"),
        opensAt=pre_market.get("opensAt"),
        closesAt=pre_market.get("closesAt"),
    )

    for label in ("up", "down"):
        if label in pre_by_label and label in poly_by_label:
            check_direct(market_context, pre_by_label[label], poly_by_label[label], min_edge)
            check_direct(market_context, poly_by_label[label], pre_by_label[label], min_edge)

    if "up" in pre_by_label and "down" in poly_by_label:
        check_lock(market_context, "LOCK_UP_PREV_DOWN_POLY", pre_by_label["up"], poly_by_label["down"], min_edge)
    if "down" in pre_by_label and "up" in poly_by_label:
        check_lock(market_context, "LOCK_DOWN_PREV_UP_POLY", pre_by_label["down"], poly_by_label["up"], min_edge)

    if maker_plan:
        emit_previsao_maker_plan(
            previsao,
            poly,
            pre_market,
            poly_market,
            pre_by_label,
            poly_by_label,
            maker_margin_pct=maker_margin_pct,
            max_order_usdc=max_order_usdc,
            min_seconds_left=min_seconds_left,
            execute=execute_previsao_maker,
        )


def emit_previsao_maker_plan(
    previsao: PrevisaoClient,
    poly: PolymarketClient,
    pre_market: dict[str, Any],
    poly_market: dict[str, Any],
    pre_by_label: dict[str, Selection],
    poly_by_label: dict[str, Selection],
    maker_margin_pct: Decimal,
    max_order_usdc: Decimal,
    min_seconds_left: int,
    execute: bool = False,
) -> None:
    closes_at = parse_datetime(pre_market.get("closesAt"))
    seconds_left = None if closes_at is None else int((closes_at - datetime.now(timezone.utc)).total_seconds())
    if seconds_left is None or seconds_left <= min_seconds_left:
        output = {
            "level": "maker_plan",
            "strategy": "previsao_maker_polymarket_hedge",
            "mode": "execute" if execute else "dry_run",
            "seconds_left": seconds_left,
            "previsao_market_id": pre_market.get("id"),
            "polymarket_market_id": poly_market.get("id"),
            "quotes": [],
            "skip_reason": "round is too close to close",
        }
        if execute:
            state_path = os.getenv("BOT_STATE_PATH", "work/hedge_state.json")
            with STATE_LOCK:
                output["hedge"] = hedge_previsao_fills(previsao, poly, state_path)
                output["sync"] = cancel_all_previsao_maker_orders(previsao)
        print(json.dumps(output, ensure_ascii=False))
        return

    margin = maker_margin_pct / Decimal("100")
    plans = []
    maybe_add_maker_quote(
        plans,
        outcome="up",
        hedge_outcome="down",
        pre_selection=pre_by_label.get("up"),
        reference_selection=poly_by_label.get("up"),
        hedge_selection=poly_by_label.get("down"),
        margin=margin,
        max_order_usdc=max_order_usdc,
    )
    maybe_add_maker_quote(
        plans,
        outcome="down",
        hedge_outcome="up",
        pre_selection=pre_by_label.get("down"),
        reference_selection=poly_by_label.get("down"),
        hedge_selection=poly_by_label.get("up"),
        margin=margin,
        max_order_usdc=max_order_usdc,
    )
    plans = choose_maker_plans(plans)
    output = {
        "level": "maker_plan",
        "strategy": "previsao_maker_polymarket_hedge",
        "mode": "execute" if execute else "dry_run",
        "margin_pct": str(maker_margin_pct),
        "max_order_usdc": str(max_order_usdc),
        "seconds_left": seconds_left,
        "previsao_market_id": pre_market.get("id"),
        "polymarket_market_id": poly_market.get("id"),
        "quotes": plans,
    }
    if execute:
        state_path = os.getenv("BOT_STATE_PATH", "work/hedge_state.json")
        with STATE_LOCK:
            hedge_result = hedge_previsao_fills(previsao, poly, state_path)
            sync_result = sync_previsao_maker_orders(previsao, int(pre_market["id"]), plans, state_path)
        output["hedge"] = hedge_result
        output["sync"] = sync_result
    print(json.dumps(output, ensure_ascii=False))


def sync_previsao_maker_orders(
    previsao: PrevisaoClient,
    market_id: int,
    plans: list[dict[str, Any]],
    state_path: str | None = None,
) -> dict[str, Any]:
    state = load_bot_state(state_path) if state_path else fresh_bot_state()
    open_orders = previsao.orders(limit=200, status="OPEN") or []
    market_orders = [
        order
        for order in open_orders
        if str(order.get("marketId")) == str(market_id)
        and str(order.get("side")).upper() == "BUY"
        and str(order.get("type")).upper() == "LIMIT"
    ]
    desired = {
        str(plan["previsao_order"]["selection_id"]): plan
        for plan in plans
        if plan.get("status") == "quote" and plan.get("previsao_order")
    }
    actions: list[dict[str, Any]] = []
    max_managed_orders = int(os.getenv("BOT_MAX_OPEN_ORDERS", "1"))
    if len(market_orders) > max(max_managed_orders, len(desired)):
        actions.append(
            {
                "action": "guard_cleanup",
                "reason": "too many open Previsao orders on this market; canceling orders outside desired quote",
                "limit": max(max_managed_orders, len(desired)),
                "open_orders_seen": len(market_orders),
            }
        )

    for order in market_orders:
        selection_id = str(order.get("selectionId"))
        desired_plan = desired.get(selection_id)
        if desired_plan is None:
            actions.append(cancel_existing_order(previsao, order, "no desired quote"))
            continue
        desired_order = desired_plan["previsao_order"]
        if order_matches_desired(order, desired_order):
            remember_previsao_order(state, order, desired_plan)
            actions.append({"action": "keep", "order_id": order.get("id"), "selection_id": selection_id})
            desired.pop(selection_id, None)
            continue
        actions.append(cancel_existing_order(previsao, order, "price/amount changed"))

    for plan in desired.values():
        desired_order = plan["previsao_order"]
        try:
            created = previsao.create_limit_buy(
                selection_id=desired_order["selection_id"],
                price=dec(desired_order["price"]),
                amount=dec(desired_order["amount"]),
            )
            actions.append(
                {
                    "action": "create",
                    "selection_id": desired_order["selection_id"],
                    "price": desired_order["price"],
                    "amount": desired_order["amount"],
                    "result": summarize_order_result(created),
                }
            )
            remember_previsao_order(state, created, plan)
        except Exception as exc:
            actions.append({"action": "create_failed", "selection_id": desired_order["selection_id"], "error": str(exc)})

    if state_path:
        save_bot_state(state_path, state)
    return {"open_orders_seen": len(market_orders), "actions": actions}


def cancel_all_previsao_maker_orders(previsao: PrevisaoClient) -> dict[str, Any]:
    open_orders = previsao.orders(limit=200, status="OPEN") or []
    actions = []
    for order in open_orders:
        if str(order.get("side")).upper() == "BUY" and str(order.get("type")).upper() == "LIMIT":
            actions.append(cancel_existing_order(previsao, order, "bot paused"))
    return {"open_orders_seen": len(open_orders), "actions": actions}


def set_remote_bot_enabled(enabled: bool) -> dict[str, Any]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY")
    token = os.getenv("DASHBOARD_DATA_TOKEN")
    if not url or not key or not token:
        return {"updated": False, "reason": "missing remote config env"}
    endpoint = f"{url.rstrip('/')}/rest/v1/bot_dashboard_config?id=eq.1&select=id,bot_enabled"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "x-bot-dashboard-token": token,
        "Prefer": "return=representation",
    }
    rows = http_json("PATCH", endpoint, payload={"bot_enabled": enabled}, headers=headers) or []
    return {"updated": True, "rows": rows}


def load_bot_state(path: str | None) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return fresh_bot_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        raw.setdefault("orders", {})
        raw.setdefault("hedged_trades", {})
        raw.setdefault("pending_hedges", [])
        raw.setdefault("polymarket_user_events", [])
        raw.setdefault("completed_hedge_trade_ids", [])
        return raw
    except Exception:
        return fresh_bot_state()


def fresh_bot_state() -> dict[str, Any]:
    return {
        "orders": {},
        "hedged_trades": {},
        "pending_hedges": [],
        "polymarket_user_events": [],
        "completed_hedge_trade_ids": [],
    }


def save_bot_state(path: str, state: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)
    publish_dashboard_state(state)


def remember_previsao_order(state: dict[str, Any], order: Any, plan: dict[str, Any]) -> None:
    if not isinstance(order, dict):
        return
    order_id = order.get("orderId") or order.get("id")
    if order_id is None:
        return
    hedge = plan.get("hedge_after_fill", {})
    previsao_order = plan.get("previsao_order", {})
    quote_price = dec(previsao_order.get("price", order.get("price")))
    max_hedge_price = max(ZERO, ONE - quote_price - Decimal("0.01"))
    state.setdefault("orders", {})[str(order_id)] = {
        "previsao_order_id": str(order_id),
        "market_id": str(order.get("marketId", "")),
        "selection_id": str(order.get("selectionId", previsao_order.get("selection_id", ""))),
        "outcome": plan.get("outcome"),
        "previsao_price": str(quote_price),
        "hedge_outcome": hedge.get("outcome"),
        "hedge_token_id": hedge.get("token_id"),
        "hedge_min_order_size": hedge.get("min_order_size"),
        "hedge_max_price": str(quantize_price(max_hedge_price)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def sanitize_polymarket_user_event(row: dict[str, Any], event_type: str) -> dict[str, Any]:
    allowed_keys = (
        "id",
        "order_id",
        "taker_order_id",
        "maker_order_id",
        "market",
        "asset_id",
        "side",
        "status",
        "type",
        "price",
        "size",
        "size_matched",
        "outcome",
        "timestamp",
        "created_at",
        "transaction_hash",
    )
    event = {
        "event_type": event_type,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    for key in allowed_keys:
        value = row.get(key)
        if value is None:
            continue
        event[key] = value if isinstance(value, (int, float, bool)) else str(value)
    return event


def record_polymarket_user_event(state_path: str, row: dict[str, Any], event_type: str) -> None:
    if not state_path:
        return
    try:
        with STATE_LOCK:
            state = load_bot_state(state_path)
            events = state.setdefault("polymarket_user_events", [])
            events.append(sanitize_polymarket_user_event(row, event_type))
            state["polymarket_user_events"] = events[-100:]
            save_bot_state(state_path, state)
    except Exception as exc:
        emit_warning("failed to persist Polymarket user WS event", error=str(exc), event_type=event_type)


def hedge_trade_ids(row: dict[str, Any]) -> list[str]:
    raw_ids = row.get("trade_ids")
    if isinstance(raw_ids, list):
        ids = [str(value) for value in raw_ids if value is not None and str(value)]
        if ids:
            return ids
    trade_id = row.get("trade_id")
    return [str(trade_id)] if trade_id is not None and str(trade_id) else []


def completed_hedge_trade_ids(state: dict[str, Any]) -> set[str]:
    ids = {str(value) for value in state.get("completed_hedge_trade_ids", []) if value is not None}
    ids.update(str(key) for key in state.get("hedged_trades", {}).keys())
    for row in state.get("hedged_trades", {}).values():
        if isinstance(row, dict):
            ids.update(hedge_trade_ids(row))
    return ids


def remember_completed_hedge_trade_ids(state: dict[str, Any], row: dict[str, Any]) -> None:
    ids = completed_hedge_trade_ids(state)
    ids.update(hedge_trade_ids(row))
    state["completed_hedge_trade_ids"] = sorted(ids)[-1000:]


def hedge_row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("order_id", "")),
        str(row.get("selection_id", "")),
        str(row.get("token_id", "")),
        str(row.get("max_price", "")),
    )


def hedge_row_queued_at(row: dict[str, Any]) -> datetime:
    parsed = parse_datetime(row.get("queued_at"))
    return parsed or datetime.now(timezone.utc)


def combine_pending_hedge_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = hedge_row_key(row)
        if not all(key):
            passthrough.append(row)
            continue
        existing = grouped.get(key)
        if existing is None:
            copy = dict(row)
            copy["trade_ids"] = hedge_trade_ids(row)
            grouped[key] = copy
            continue
        trade_ids = list(dict.fromkeys(hedge_trade_ids(existing) + hedge_trade_ids(row)))
        shares = dec(existing.get("shares", "0")) + dec(row.get("shares", "0"))
        previsao_cost = dec(existing.get("previsao_cost", "0")) + dec(row.get("previsao_cost", "0"))
        max_price = dec(existing.get("max_price", row.get("max_price", "0")))
        hedge_cost_max = max_price * shares
        gross_profit_expected = dec(existing.get("gross_profit_expected", "0")) + dec(row.get("gross_profit_expected", "0"))
        queued_at = min(hedge_row_queued_at(existing), hedge_row_queued_at(row)).isoformat()
        existing.update(
            {
                "trade_id": "+".join(trade_ids),
                "trade_ids": trade_ids,
                "shares": str(shares),
                "previsao_price": str((previsao_cost / shares).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)) if shares > ZERO else "0",
                "previsao_cost": str(previsao_cost.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "hedge_cost_max": str(hedge_cost_max.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "gross_profit_expected": str(gross_profit_expected.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "queued_at": queued_at,
            }
        )
    return passthrough + list(grouped.values())


def wait_seconds_for_small_hedge() -> Decimal:
    return dec(os.getenv("BOT_SMALL_FILL_WAIT_SECONDS", "10"))


def should_unwind_after_polymarket_failure(error_message: str) -> bool:
    lowered = error_message.lower()
    triggers = (
        "missing current ask",
        "exceeds max",
        "below hedge",
        "below minimum hedge",
        "too small",
        "invalid amount",
        "minimum",
        "min",
    )
    return any(trigger in lowered for trigger in triggers)


def hedge_previsao_fills(previsao: PrevisaoClient, poly: PolymarketClient, state_path: str) -> dict[str, Any]:
    state = load_bot_state(state_path)
    prune_stale_watched_orders(state)
    watched = state.get("orders", {})
    actions: list[dict[str, Any]] = []
    hedged_trades = state.setdefault("hedged_trades", {})
    completed_ids = completed_hedge_trade_ids(state)
    rows_to_hedge = [
        row
        for row in state.setdefault("pending_hedges", [])
        if isinstance(row, dict) and not any(trade_id in completed_ids for trade_id in hedge_trade_ids(row))
    ]
    pending_trade_ids = {str(row.get("trade_id")) for row in rows_to_hedge if isinstance(row, dict)}
    for row in rows_to_hedge:
        pending_trade_ids.update(hedge_trade_ids(row))
    if not watched and not rows_to_hedge:
        return {"watched_orders": 0, "actions": []}
    trades = (previsao.trades(limit=100) or []) if watched else []
    for trade in sorted(trades, key=lambda row: str(row.get("id"))):
        order_id = str(trade.get("orderId", ""))
        trade_id = str(trade.get("id", ""))
        if (
            not order_id
            or order_id not in watched
            or not trade_id
            or trade_id in completed_ids
            or trade_id in pending_trade_ids
        ):
            continue
        if str(trade.get("role", "")).upper() != "MAKER":
            continue
        watched_order = watched[order_id]
        token_id = watched_order.get("hedge_token_id")
        if not token_id:
            actions.append({"action": "hedge_skipped", "trade_id": trade_id, "order_id": order_id, "reason": "missing hedge token"})
            continue
        shares = dec(trade.get("amount", "0"))
        previsao_price = dec(trade.get("price", watched_order.get("previsao_price", "0")))
        max_price = dec(watched_order.get("hedge_max_price", "0"))
        if shares <= ZERO or max_price <= ZERO:
            actions.append({"action": "hedge_skipped", "trade_id": trade_id, "order_id": order_id, "reason": "bad amount or price"})
            continue
        previsao_cost = previsao_price * shares
        hedge_cost_max = max_price * shares
        gross_profit_expected = (ONE - previsao_price - max_price) * shares
        rows_to_hedge.append(
            {
                "trade_id": trade_id,
                "order_id": order_id,
                "market_id": watched_order.get("market_id") or trade.get("marketId"),
                "selection_id": watched_order.get("selection_id") or trade.get("selectionId"),
                "token_id": token_id,
                "shares": str(shares),
                "previsao_outcome": watched_order.get("outcome"),
                "previsao_price": str(previsao_price),
                "previsao_cost": str(previsao_cost.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "hedge_outcome": watched_order.get("hedge_outcome"),
                "max_price": str(max_price),
                "hedge_min_order_size": watched_order.get("hedge_min_order_size"),
                "hedge_cost_max": str(hedge_cost_max.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "gross_profit_expected": str(gross_profit_expected.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        pending_trade_ids.add(trade_id)
        actions.append(
            {
                "action": "hedge_detected",
                "trade_id": trade_id,
                "order_id": order_id,
                "shares": str(shares),
                "max_price": str(max_price),
            }
        )

    rows_to_hedge = combine_pending_hedge_rows(rows_to_hedge)
    remaining_pending: list[dict[str, Any]] = []
    hard_buy_cap = dec(os.getenv("POLYMARKET_MARKET_BUY_MAX_PRICE", "0.99"))
    hedge_slippage = dec(os.getenv("POLYMARKET_HEDGE_SLIPPAGE", "0.03"))
    hedge_circuit_breaker = os.getenv("POLYMARKET_HEDGE_CIRCUIT_BREAKER", "1") != "0"
    min_market_buy_usdc = dec(os.getenv("POLYMARKET_MIN_MARKET_BUY_USDC", "1"))
    for row in rows_to_hedge:
        token_id = str(row.get("token_id"))
        shares = dec(row.get("shares", "0"))
        quote_max_price = dec(row.get("max_price", "0"))
        max_acceptable_price = quantize_price(min(hard_buy_cap, quote_max_price + hedge_slippage))
        execution_price = max_acceptable_price
        hedge_order_shares = quantize_poly_shares(shares)
        bumped_to_minimum = False
        try:
            book = poly.books([token_id]).get(token_id)
            current_ask = book.ask if book else None
            if current_ask is None or current_ask.price <= ZERO:
                raise RuntimeError("Polymarket hedge skipped: missing current ask")
            execution_price = quantize_price(current_ask.price)
            if execution_price > max_acceptable_price:
                raise RuntimeError(
                    f"Polymarket hedge skipped: current ask {execution_price} exceeds max {max_acceptable_price}"
                )
            if current_ask.size < hedge_order_shares:
                raise RuntimeError(
                    f"Polymarket hedge skipped: current ask size {current_ask.size} below hedge {hedge_order_shares}"
                )
            hedge_spend_usdc = (hedge_order_shares * execution_price).quantize(Decimal("0.01"), rounding=ROUND_UP)
            if min_market_buy_usdc > ZERO and hedge_spend_usdc < min_market_buy_usdc:
                wait_seconds = wait_seconds_for_small_hedge()
                queued_at = hedge_row_queued_at(row)
                age_seconds = Decimal(str(max(0.0, (datetime.now(timezone.utc) - queued_at).total_seconds())))
                if age_seconds < wait_seconds:
                    remaining_pending.append(row)
                    actions.append(
                        {
                            "action": "wait_for_more_previsao_fill",
                            "trade_id": row.get("trade_id"),
                            "trade_ids": hedge_trade_ids(row),
                            "shares": str(shares),
                            "hedge_spend_usdc": str(hedge_spend_usdc),
                            "minimum_market_buy_usdc": str(min_market_buy_usdc),
                            "age_seconds": str(age_seconds.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                            "wait_seconds": str(wait_seconds),
                        }
                    )
                    continue
                try:
                    unwound = unwind_previsao_fill_with_market_sell(previsao, row)
                    hedged_trades[str(row.get("trade_id"))] = unwound
                    remember_completed_hedge_trade_ids(state, unwound)
                    actions.append(
                        {
                            "action": "unwind_sell_previsao_below_polymarket_market_minimum",
                            "trade_id": row.get("trade_id"),
                            "trade_ids": hedge_trade_ids(row),
                            "shares": str(shares),
                            "hedge_spend_usdc": str(hedge_spend_usdc),
                            "minimum_market_buy_usdc": str(min_market_buy_usdc),
                            "proceeds": unwound.get("previsao_unwind_proceeds"),
                            "pnl": unwound.get("gross_profit_final"),
                        }
                    )
                    continue
                except Exception as exc:
                    remaining_pending.append(row)
                    actions.append(
                        {
                            "action": "unwind_previsao_below_polymarket_market_minimum_failed",
                            "trade_id": row.get("trade_id"),
                            "trade_ids": hedge_trade_ids(row),
                            "shares": str(shares),
                            "hedge_spend_usdc": str(hedge_spend_usdc),
                            "minimum_market_buy_usdc": str(min_market_buy_usdc),
                            "error": str(exc),
                        }
                    )
                    continue
            result = poly.buy_market_usdc(
                token_id=token_id,
                amount_usdc=hedge_spend_usdc,
                max_price=max_acceptable_price,
            )
            actual_hedge_shares, actual_hedge_cost = parse_polymarket_buy_result(
                result,
                fallback_shares=hedge_order_shares,
                fallback_cost=hedge_spend_usdc,
            )
            min_fill_ratio = dec(os.getenv("POLYMARKET_MIN_HEDGE_FILL_RATIO", "0.98"))
            underfilled = shares > ZERO and actual_hedge_shares < (shares * min_fill_ratio)
            overfilled = hedge_result_overfilled(actual_hedge_shares, shares, hedge_order_shares)
            hedged_at = datetime.now(timezone.utc).isoformat()
            previsao_cost = dec(row.get("previsao_cost", "0"))
            locked_shares = min(shares, actual_hedge_shares)
            gross_profit_worst = locked_shares - previsao_cost - actual_hedge_cost
            hedged_trades[str(row.get("trade_id"))] = {
                **row,
                "execution_max_price": str(execution_price),
                "requested_hedge_shares": str(shares),
                "order_hedge_shares": str(hedge_order_shares),
                "actual_hedge_shares": str(actual_hedge_shares),
                "actual_hedge_cost": str(actual_hedge_cost),
                "extra_hedge_shares": str((actual_hedge_shares - shares).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)),
                "gross_profit_worst": str(gross_profit_worst.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "hedge_underfilled": underfilled,
                "hedge_overfilled": overfilled,
                "hedge_bumped_to_minimum": bumped_to_minimum,
                "result": result,
                "hedged_at": hedged_at,
            }
            remember_completed_hedge_trade_ids(state, row)
            if overfilled:
                emit_warning(
                    "Polymarket hedge overfilled requested shares",
                    trade_id=row.get("trade_id"),
                    requested_shares=str(shares),
                    order_shares=str(hedge_order_shares),
                    actual_shares=str(actual_hedge_shares),
                    actual_cost=str(actual_hedge_cost),
                )
            circuit_breaker = None
            if underfilled and hedge_circuit_breaker:
                circuit_breaker = cancel_all_previsao_maker_orders(previsao)
                stop_path = os.getenv("BOT_STOP_PATH", "work/STOP_BOT")
                with open(stop_path, "w", encoding="utf-8") as handle:
                    handle.write(
                        f"hedge underfilled at {hedged_at}: trade {row.get('trade_id')} "
                        f"needed {shares}, got {actual_hedge_shares}\n"
                    )
            actions.append(
                {
                    "action": "hedge_buy_polymarket",
                    "trade_id": row.get("trade_id"),
                    "shares": str(shares),
                    "order_shares": str(hedge_order_shares),
                    "quote_max_price": str(quote_max_price),
                    "current_ask_price": str(execution_price),
                    "execution_max_price": str(execution_price),
                    "underfilled": underfilled,
                    "overfilled": overfilled,
                    "bumped_to_minimum": bumped_to_minimum,
                    "circuit_breaker": circuit_breaker,
                    "result": result,
                }
            )
        except Exception as exc:
            error_message = str(exc)
            circuit_breaker = cancel_all_previsao_maker_orders(previsao) if hedge_circuit_breaker else None
            if hedge_circuit_breaker:
                stop_path = os.getenv("BOT_STOP_PATH", "work/STOP_BOT")
                try:
                    with open(stop_path, "w", encoding="utf-8") as handle:
                        handle.write(f"hedge failed at {datetime.now(timezone.utc).isoformat()}: {error_message}\n")
                except Exception as stop_exc:
                    emit_warning("failed to write stop file after hedge failure", error=str(stop_exc))
            if should_unwind_after_polymarket_failure(error_message):
                try:
                    unwound = unwind_previsao_fill_with_market_sell(previsao, row)
                    unwound["unwind_reason"] = f"polymarket_hedge_failed: {error_message[:180]}"
                    hedged_trades[str(row.get("trade_id"))] = unwound
                    remember_completed_hedge_trade_ids(state, unwound)
                    actions.append(
                        {
                            "action": "unwind_sell_previsao_after_poly_failure",
                            "trade_id": row.get("trade_id"),
                            "trade_ids": hedge_trade_ids(row),
                            "shares": str(shares),
                            "quote_max_price": str(quote_max_price),
                            "execution_max_price": str(locals().get("execution_price", max_acceptable_price)),
                            "proceeds": unwound.get("previsao_unwind_proceeds"),
                            "pnl": unwound.get("gross_profit_final"),
                            "polymarket_error": error_message,
                            "circuit_breaker": circuit_breaker,
                        }
                    )
                    continue
                except Exception as unwind_exc:
                    remaining_pending.append(row)
                    actions.append(
                        {
                            "action": "unwind_previsao_after_poly_failure_failed",
                            "trade_id": row.get("trade_id"),
                            "trade_ids": hedge_trade_ids(row),
                            "shares": str(shares),
                            "quote_max_price": str(quote_max_price),
                            "execution_max_price": str(locals().get("execution_price", max_acceptable_price)),
                            "polymarket_error": error_message,
                            "unwind_error": str(unwind_exc),
                            "circuit_breaker": circuit_breaker,
                        }
                    )
                    continue
            if "invalid token id" in error_message.lower():
                hedged_at = datetime.now(timezone.utc).isoformat()
                hedged_trades[str(row.get("trade_id"))] = {
                    **row,
                    "execution_max_price": str(execution_price),
                    "actual_hedge_shares": "0",
                    "actual_hedge_cost": "0",
                    "extra_hedge_shares": str((ZERO - dec(row.get("shares", "0"))).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)),
                    "gross_profit_worst": str((ZERO - dec(row.get("previsao_cost", "0"))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                    "result": {"error": error_message, "abandoned": True},
                    "hedged_at": hedged_at,
                }
                remember_completed_hedge_trade_ids(state, row)
            else:
                remaining_pending.append(row)
            actions.append(
                {
                    "action": "hedge_failed",
                    "trade_id": row.get("trade_id"),
                    "shares": str(shares),
                    "quote_max_price": str(quote_max_price),
                    "execution_max_price": str(locals().get("execution_price", max_acceptable_price)),
                    "error": error_message,
                    "circuit_breaker": circuit_breaker,
                }
            )
    state["pending_hedges"] = remaining_pending
    save_bot_state(state_path, state)
    return {"watched_orders": len(watched), "actions": actions}


def extract_previsao_market_sell_proceeds(result: Any, fallback_proceeds: Decimal) -> Decimal:
    if not isinstance(result, dict):
        return fallback_proceeds
    for key in (
        "proceeds",
        "received",
        "total",
        "filledTotal",
        "filled_total",
        "executedTotal",
        "executed_total",
        "value",
    ):
        value = result.get(key)
        if value is not None:
            try:
                parsed = dec(value)
            except ValueError:
                continue
            if parsed >= ZERO:
                return parsed
    trades = result.get("trades")
    if isinstance(trades, list):
        total = ZERO
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            amount_raw = trade.get("amount") or trade.get("shares") or trade.get("size")
            price_raw = trade.get("price")
            if amount_raw is None or price_raw is None:
                continue
            try:
                total += dec(amount_raw) * dec(price_raw)
            except ValueError:
                continue
        if total > ZERO:
            return total
    return fallback_proceeds


def estimate_previsao_market_sell(book_raw: dict[str, Any], selection_id: str, shares: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    selection_book = book_raw.get("books", {}).get(str(selection_id), {})
    levels: list[Level] = []
    for raw in selection_book.get("bids", []):
        try:
            level = Level(price=dec(raw["price"]), size=dec(raw.get("size", raw.get("amount", "0"))))
        except (KeyError, ValueError):
            continue
        if level.price > ZERO and level.size > ZERO:
            levels.append(level)
    levels.sort(key=lambda level: level.price, reverse=True)
    remaining = shares
    proceeds = ZERO
    filled = ZERO
    for level in levels:
        if remaining <= ZERO:
            break
        take = min(remaining, level.size)
        proceeds += take * level.price
        filled += take
        remaining -= take
    best_price = levels[0].price if levels else ZERO
    return filled, proceeds.quantize(Decimal("0.000001"), rounding=ROUND_DOWN), best_price


def unwind_previsao_fill_with_market_sell(previsao: PrevisaoClient, row: dict[str, Any]) -> dict[str, Any]:
    shares = dec(row.get("shares", "0"))
    market_id = row.get("market_id")
    selection_id = row.get("selection_id")
    if shares <= ZERO or not market_id or not selection_id:
        raise RuntimeError("Previsao unwind skipped: missing market/selection/shares")
    book_raw = previsao.orderbook(int(market_id), int(os.getenv("PREVISAO_UNWIND_BOOK_DEPTH", "20")))
    filled_estimate, estimated_proceeds, best_bid_price = estimate_previsao_market_sell(book_raw, str(selection_id), shares)
    if filled_estimate < shares:
        raise RuntimeError(f"Previsao unwind skipped: bid depth {filled_estimate} below {shares}")
    min_sell_usdc = dec(os.getenv("PREVISAO_MIN_MARKET_SELL_USDC", "0.01"))
    if estimated_proceeds < min_sell_usdc:
        raise RuntimeError(f"Previsao unwind skipped: estimated proceeds {estimated_proceeds} below {min_sell_usdc}")
    result = previsao.create_market_sell(selection_id=str(selection_id), amount=shares)
    proceeds = extract_previsao_market_sell_proceeds(result, estimated_proceeds)
    cost = dec(row.get("previsao_cost", "0"))
    pnl = proceeds - cost
    return {
        **row,
        "unwind_venue": "previsao",
        "unwind_reason": "below_polymarket_limit_minimum",
        "previsao_unwind_price_estimate": str(best_bid_price),
        "previsao_unwind_proceeds": str(proceeds),
        "previsao_unwind_result": result,
        "actual_hedge_shares": "0",
        "actual_hedge_cost": "0",
        "gross_profit_worst": str(pnl.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "gross_profit_final": str(pnl.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "final_status": "unwound_previsao",
        "hedged_at": datetime.now(timezone.utc).isoformat(),
    }


def summarize_hedged_operations(state_path: str) -> list[dict[str, Any]]:
    state = load_bot_state(state_path)
    return summarize_hedged_operations_from_state(state)


def summarize_hedged_operations_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    operations = []
    for trade_id, row in state.get("hedged_trades", {}).items():
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        shares = dec(row.get("shares", "0"))
        previsao_cost = dec(row.get("previsao_cost", "0"))
        if row.get("unwind_venue") == "previsao":
            proceeds = dec(row.get("previsao_unwind_proceeds", "0"))
            pnl = proceeds - previsao_cost
            operations.append(
                {
                    "trade_id": trade_id,
                    "order_id": row.get("order_id"),
                    "market_id": row.get("market_id"),
                    "selection_id": row.get("selection_id"),
                    "previsao": row.get("previsao_outcome"),
                    "polymarket": "zerado na Previsao",
                    "shares": row.get("shares"),
                    "previsao_price": row.get("previsao_price"),
                    "polymarket_max_price": None,
                    "previsao_cost": row.get("previsao_cost"),
                    "polymarket_cost_max": "0",
                    "polymarket_cost_real": "0.000000",
                    "extra_hedge_shares": "0.000000",
                    "gross_profit_expected": row.get("gross_profit_expected"),
                    "gross_profit_worst": str(pnl.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                    "gross_profit_final": str(pnl.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                    "final_status": "unwound_previsao",
                    "previsao_unwind_proceeds": str(proceeds.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)),
                    "previsao_unwind_price_estimate": row.get("previsao_unwind_price_estimate"),
                    "hedge_underfilled": False,
                    "hedge_overfilled": False,
                    "hedged_at": row.get("hedged_at"),
                }
            )
            continue
        hedge_cost_max = dec(row.get("hedge_cost_max", "0"))
        actual_hedge_shares, actual_hedge_cost = parse_polymarket_buy_result(
            result,
            fallback_shares=dec(row.get("actual_hedge_shares") or shares),
            fallback_cost=dec(row.get("actual_hedge_cost") or hedge_cost_max),
        )
        locked_shares = min(shares, actual_hedge_shares)
        gross_profit_real = locked_shares - previsao_cost - actual_hedge_cost
        extra_hedge_shares = actual_hedge_shares - shares
        operations.append(
            {
                "trade_id": trade_id,
                "order_id": row.get("order_id"),
                "market_id": row.get("market_id"),
                "selection_id": row.get("selection_id"),
                "previsao": row.get("previsao_outcome"),
                "polymarket": row.get("hedge_outcome"),
                "shares": row.get("shares"),
                "previsao_price": row.get("previsao_price"),
                "polymarket_max_price": row.get("max_price"),
                "previsao_cost": row.get("previsao_cost"),
                "polymarket_cost_max": row.get("hedge_cost_max"),
                "polymarket_cost_real": str(actual_hedge_cost.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)),
                "extra_hedge_shares": str(extra_hedge_shares.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)),
                "gross_profit_expected": row.get("gross_profit_expected"),
                "gross_profit_worst": str(gross_profit_real.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "hedge_underfilled": row.get("hedge_underfilled") is True,
                "hedge_overfilled": row.get("hedge_overfilled") is True
                or hedge_result_overfilled(
                    actual_hedge_shares,
                    shares,
                    dec(row.get("order_hedge_shares") or row.get("requested_hedge_shares") or shares),
                ),
                "hedged_at": row.get("hedged_at"),
                **resolve_operation_final_pnl(row, actual_hedge_shares, actual_hedge_cost),
            }
        )
    operations.sort(key=lambda row: str(row.get("hedged_at", "")), reverse=True)
    return operations[:50]


def consecutive_final_losses(state: dict[str, Any]) -> list[dict[str, Any]]:
    losses: list[dict[str, Any]] = []
    for operation in summarize_hedged_operations_from_state(state):
        if operation.get("final_status") != "resolved":
            continue
        pnl = dec(operation.get("gross_profit_final", "0"))
        if pnl >= ZERO:
            break
        losses.append(operation)
    return losses


def enforce_loss_streak_breaker(
    previsao: PrevisaoClient,
    state: dict[str, Any],
    state_path: str,
    stop_path: str,
) -> dict[str, Any] | None:
    threshold = int(os.getenv("BOT_MAX_CONSECUTIVE_FINAL_LOSSES", "5"))
    if threshold <= 0:
        return None
    losses = consecutive_final_losses(state)
    if len(losses) < threshold:
        return None

    triggered_at = datetime.now(timezone.utc).isoformat()
    trade_ids = [str(row.get("trade_id")) for row in losses[:threshold]]
    reason = f"{threshold} consecutive resolved losses"
    cancel_result = cancel_all_previsao_maker_orders(previsao)
    remote_result: dict[str, Any]
    try:
        remote_result = set_remote_bot_enabled(False)
    except Exception as exc:
        remote_result = {"updated": False, "error": str(exc)}

    directory = os.path.dirname(stop_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(stop_path, "w", encoding="utf-8") as handle:
        handle.write(f"loss streak breaker at {triggered_at}: {reason}; trades={','.join(trade_ids)}\n")

    breaker = {
        "triggered_at": triggered_at,
        "reason": reason,
        "threshold": threshold,
        "loss_count": len(losses),
        "trade_ids": trade_ids,
        "cancel": cancel_result,
        "remote_config": remote_result,
    }
    state["loss_streak_breaker"] = breaker
    save_bot_state(state_path, state)
    emit_warning("loss streak breaker stopped bot", **{k: v for k, v in breaker.items() if k not in {"cancel", "remote_config"}})
    return breaker


def enrich_hedged_trade_settlements(previsao: PrevisaoClient, state_path: str) -> dict[str, Any]:
    state = load_bot_state(state_path)
    hedged_trades = state.get("hedged_trades", {})
    if not hedged_trades:
        return state
    changed = False
    try:
        trades = previsao.trades(limit=100) or []
    except Exception as exc:
        emit_warning("failed to load Previsao trades for settlement enrichment", error=str(exc))
        trades = []
    trades_by_id = {str(trade.get("id")): trade for trade in trades if isinstance(trade, dict)}
    markets: dict[str, dict[str, Any]] = {}
    for trade_id, row in hedged_trades.items():
        if not isinstance(row, dict):
            continue
        trade = trades_by_id.get(str(trade_id))
        if trade:
            for key, source_key in (("market_id", "marketId"), ("selection_id", "selectionId")):
                if not row.get(key) and trade.get(source_key) is not None:
                    row[key] = str(trade.get(source_key))
                    changed = True
        market_id = row.get("market_id")
        if not market_id:
            continue
        if str(market_id) not in markets:
            try:
                markets[str(market_id)] = http_json("GET", f"{previsao.base_url}/markets/{market_id}")["data"]
            except Exception as exc:
                emit_warning("failed to load Previsao market for settlement enrichment", market_id=market_id, error=str(exc))
                markets[str(market_id)] = {}
        market = markets.get(str(market_id), {})
        result_selection_id = market.get("resultSelectionId")
        if result_selection_id is not None and str(row.get("result_selection_id")) != str(result_selection_id):
            row["result_selection_id"] = str(result_selection_id)
            row["resolved_at"] = market.get("resolvedAt")
            changed = True
    if changed:
        save_bot_state(state_path, state)
    return state


def build_runtime_health(
    poly: PolymarketClient | None = None,
    previsao_public_ws: PrevisaoWsPublicMonitor | None = None,
    previsao_ws: PrevisaoWsHedgeAccelerator | None = None,
    polymarket_user_ws: PolymarketWsUserMonitor | None = None,
) -> dict[str, Any]:
    return {
        "previsao_public_ws": previsao_public_ws.snapshot() if previsao_public_ws else {"enabled": False},
        "previsao_ws": previsao_ws.snapshot() if previsao_ws else {"enabled": False},
        "polymarket_market_ws": poly.book_cache.snapshot() if poly and poly.book_cache else {"enabled": False},
        "polymarket_user_ws": polymarket_user_ws.snapshot() if polymarket_user_ws else {"enabled": False},
    }


def publish_dashboard_state(
    state: dict[str, Any],
    runtime_health: dict[str, Any] | None = None,
) -> None:
    global LAST_DASHBOARD_PUBLISH_AT
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY")
    token = os.getenv("DASHBOARD_DATA_TOKEN")
    now = time.time()
    if not url or not key or not token or now - LAST_DASHBOARD_PUBLISH_AT < 10:
        return
    LAST_DASHBOARD_PUBLISH_AT = now
    payload = {
        "source": "local-bot",
        "payload": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "operations": summarize_hedged_operations_from_state(state),
            "pending_hedges": state.get("pending_hedges", []),
            "polymarket_user_events": state.get("polymarket_user_events", [])[-50:],
            "health": {
                "ws": runtime_health or {},
                "pending_hedge_count": len(state.get("pending_hedges", [])),
                "last_hedge": (summarize_hedged_operations_from_state(state) or [None])[0],
            },
        },
    }
    endpoint = f"{url.rstrip('/')}/rest/v1/bot_dashboard_snapshots"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "x-bot-dashboard-token": token,
        "Prefer": "return=minimal",
    }
    try:
        http_json("POST", endpoint, payload=payload, headers=headers)
    except Exception as exc:
        emit_warning("failed to publish dashboard state", error=str(exc))


def load_remote_bot_config(default_margin: Decimal, default_max_order: Decimal, default_min_seconds: int) -> tuple[Decimal, Decimal, int, bool]:
    min_seconds_floor = int(os.getenv("BOT_MIN_SECONDS_LEFT_FLOOR", "20"))
    default_min_seconds = max(default_min_seconds, min_seconds_floor)
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY")
    token = os.getenv("DASHBOARD_DATA_TOKEN")
    if not url or not key or not token:
        return default_margin, default_max_order, default_min_seconds, True
    endpoint = f"{url.rstrip('/')}/rest/v1/bot_dashboard_config?select=margin_pct,max_order_usdc,min_seconds_left,bot_enabled&id=eq.1&limit=1"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "x-bot-dashboard-token": token,
    }
    try:
        rows = http_json("GET", endpoint, headers=headers) or []
        if not rows:
            return default_margin, default_max_order, default_min_seconds, True
        row = rows[0]
        return (
            dec(row.get("margin_pct", default_margin)),
            dec(row.get("max_order_usdc", default_max_order)),
            max(int(row.get("min_seconds_left", default_min_seconds)), min_seconds_floor),
            row.get("bot_enabled") is not False,
        )
    except Exception as exc:
        emit_warning("failed to load remote bot config", error=str(exc))
        return default_margin, default_max_order, default_min_seconds, True


def prune_stale_watched_orders(state: dict[str, Any], max_age_seconds: int = 900) -> None:
    now = datetime.now(timezone.utc)
    kept = {}
    for order_id, row in state.get("orders", {}).items():
        created_at = parse_datetime(row.get("created_at")) if isinstance(row, dict) else None
        if created_at is None or (now - created_at).total_seconds() <= max_age_seconds:
            kept[order_id] = row
    state["orders"] = kept


def order_matches_desired(order: dict[str, Any], desired_order: dict[str, Any]) -> bool:
    return (
        str(order.get("selectionId")) == str(desired_order["selection_id"])
        and dec(order.get("price")) == dec(desired_order["price"])
        and dec(order.get("amountRemaining", order.get("amount"))) == dec(desired_order["amount"])
    )


def cancel_existing_order(previsao: PrevisaoClient, order: dict[str, Any], reason: str) -> dict[str, Any]:
    try:
        result = previsao.cancel_order(order["id"])
        return {"action": "cancel", "order_id": order.get("id"), "reason": reason, "result": summarize_order_result(result)}
    except Exception as exc:
        return {"action": "cancel_failed", "order_id": order.get("id"), "reason": reason, "error": str(exc)}


def summarize_order_result(order: Any) -> Any:
    if not isinstance(order, dict):
        return order
    keys = ["orderId", "id", "marketId", "selectionId", "status", "side", "type", "price", "amount", "filledAmount"]
    return {key: order.get(key) for key in keys if key in order}


def maybe_add_maker_quote(
    plans: list[dict[str, Any]],
    outcome: str,
    hedge_outcome: str,
    pre_selection: Selection | None,
    reference_selection: Selection | None,
    hedge_selection: Selection | None,
    margin: Decimal,
    max_order_usdc: Decimal,
) -> None:
    if pre_selection is None or hedge_selection is None or hedge_selection.book.ask is None:
        plans.append({"outcome": outcome, "status": "skipped", "reason": "missing hedge ask"})
        return

    hedge_ask = hedge_selection.book.ask
    reference_bid = reference_selection.book.bid if reference_selection is not None else None
    hedge_complement_limit = ONE - hedge_ask.price
    no_margin_limit = min(hedge_complement_limit, reference_bid.price) if reference_bid is not None else hedge_complement_limit
    quote_price = quantize_price(no_margin_limit * (ONE - margin))
    min_quote_price = dec(os.getenv("BOT_MIN_QUOTE_PRICE", "0.05"))
    if quote_price <= ZERO:
        plans.append(
            {
                "outcome": outcome,
                "status": "skipped",
                "reason": "hedge ask leaves no positive quote after margin",
                "hedge_ask": str(hedge_ask.price),
            }
        )
        return
    if quote_price < min_quote_price:
        plans.append(
            {
                "outcome": outcome,
                "status": "skipped",
                "reason": "quote price too small",
                "price": str(quote_price),
                "minimum_price": str(min_quote_price),
            }
        )
        return

    hard_cap_usdc = max_order_usdc
    quote_shares_by_budget = (max_order_usdc / quote_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    quote_shares_by_hard_cap = (hard_cap_usdc / quote_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    hedgeable_shares = min(quote_shares_by_budget, hedge_ask.size)
    if hedgeable_shares <= ZERO:
        plans.append({"outcome": outcome, "status": "skipped", "reason": "zero hedgeable shares"})
        return
    poly_min_market_buy_usdc = dec(os.getenv("POLYMARKET_MIN_MARKET_BUY_USDC", "1"))
    estimated_hedge_spend = hedgeable_shares * hedge_ask.price
    if (
        poly_min_market_buy_usdc > ZERO
        and estimated_hedge_spend < poly_min_market_buy_usdc
        and poly_min_market_buy_usdc > (hedge_ask.size * hedge_ask.price)
    ):
        plans.append(
            {
                "outcome": outcome,
                "status": "skipped",
                "reason": "polymarket market minimum not reachable from visible liquidity",
                "estimated_hedge_spend": str(estimated_hedge_spend.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                "minimum_usdc": str(poly_min_market_buy_usdc),
            }
        )
        return
    min_previsao_limit_shares = dec(os.getenv("PREVISAO_MIN_LIMIT_ORDER_SHARES", "5"))
    if min_previsao_limit_shares > ZERO and hedgeable_shares < min_previsao_limit_shares:
        min_shares = min_previsao_limit_shares
        if min_shares <= quote_shares_by_hard_cap and min_shares <= hedge_ask.size:
            hedgeable_shares = min_shares
        else:
            plans.append(
                {
                    "outcome": outcome,
                    "status": "skipped",
                    "reason": "previsao minimum limit shares not met",
                    "price": str(quote_price),
                    "available_shares": str(hedgeable_shares),
                    "minimum_shares": str(min_shares),
                }
            )
            return

    combined_cost = quote_price + hedge_ask.price
    plans.append(
        {
            "outcome": outcome,
            "status": "quote",
            "previsao_order": {
                "selection_id": pre_selection.selection_id,
                "side": "BUY",
                "type": "LIMIT",
                "price": str(quote_price),
                "amount": str(hedgeable_shares),
                "max_cost": str((quote_price * hedgeable_shares).quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
            },
            "hedge_after_fill": {
                "venue": "polymarket",
                "outcome": hedge_outcome,
                "token_id": hedge_selection.token_id,
                "max_price_now": str(hedge_ask.price),
                "same_outcome_bid": None if reference_bid is None else str(reference_bid.price),
                "no_margin_limit": str(no_margin_limit),
                "visible_size": str(hedge_ask.size),
                "min_market_buy_usdc": str(poly_min_market_buy_usdc),
            },
            "combined_cost_if_hedged_now": str(combined_cost),
            "gross_edge_if_hedged_now": str(ONE - combined_cost),
        }
    )


def is_expired(market: dict[str, Any]) -> bool:
    closes_at = market.get("closesBettingAt") or market.get("closesAt")
    if not closes_at:
        return False
    normalized = str(closes_at).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return False


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    raise ValueError(f"expected JSON list, got {value!r}")


def normalize_outcome_label(value: Any) -> str:
    label = str(value or "").strip().lower()
    if label in {"up", "yes"}:
        return "up"
    if label in {"down", "no"}:
        return "down"
    return label


def format_book(book: Book) -> dict[str, Any]:
    mid = None
    spread = None
    if book.bid is not None and book.ask is not None:
        mid = (book.bid.price + book.ask.price) / Decimal("2")
        spread = book.ask.price - book.bid.price
    return {
        "bid": None if book.bid is None else {"price": str(book.bid.price), "size": str(book.bid.size)},
        "ask": None if book.ask is None else {"price": str(book.ask.price), "size": str(book.ask.size)},
        "mid": None if mid is None else str(mid.quantize(Decimal("0.0001"))),
        "spread": None if spread is None else str(spread),
    }


def quantize_price(price: Decimal, tick: Decimal = Decimal("0.01")) -> Decimal:
    return price.quantize(tick, rounding=ROUND_DOWN)


def quantize_poly_shares(shares: Decimal) -> Decimal:
    return shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def quantize_poly_buy_shares(shares: Decimal, price: Decimal) -> Decimal:
    if price <= ZERO:
        return quantize_poly_shares(shares)
    price_cents = int((price * Decimal("100")).to_integral_value(rounding=ROUND_DOWN))
    if price_cents <= 0:
        return quantize_poly_shares(shares)
    share_cents = int((shares * Decimal("100")).to_integral_value(rounding=ROUND_UP))
    step_cents = 100 // math.gcd(price_cents, 100)
    rounded_share_cents = ((share_cents + step_cents - 1) // step_cents) * step_cents
    return Decimal(rounded_share_cents) / Decimal("100")


def check_direct(market: dict[str, Any], buy: Selection, sell: Selection, min_edge: Decimal) -> None:
    if buy.book.ask is None or sell.book.bid is None:
        return
    edge = sell.book.bid.price - buy.book.ask.price
    if edge < min_edge:
        return
    emit_opportunity(
        f"DIRECT_BUY_{buy.venue.upper()}_SELL_{sell.venue.upper()}",
        market,
        edge,
        max_shares_for_direct(buy.book.ask, sell.book.bid),
        {
            "outcome": buy.label,
            "buy_price": str(buy.book.ask.price),
            "sell_price": str(sell.book.bid.price),
            "note": "requires inventory or reliable sell-side execution",
        },
    )


def check_lock(market: dict[str, Any], kind: str, leg_a: Selection, leg_b: Selection, min_edge: Decimal) -> None:
    if leg_a.book.ask is None or leg_b.book.ask is None:
        return
    cost = leg_a.book.ask.price + leg_b.book.ask.price
    edge = ONE - cost
    if edge < min_edge:
        return
    emit_opportunity(
        kind,
        market,
        edge,
        max_shares_for_lock(leg_a.book.ask, leg_b.book.ask),
        {
            "leg_a": {"venue": leg_a.venue, "outcome": leg_a.label, "price": str(leg_a.book.ask.price)},
            "leg_b": {"venue": leg_b.venue, "outcome": leg_b.label, "price": str(leg_b.book.ask.price)},
            "combined_cost": str(cost),
        },
    )


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Dry-run Previsao <> Polymarket arbitrage scanner")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--per-page", type=int, default=25)
    parser.add_argument("--depth", type=int, default=50)
    parser.add_argument("--min-edge-bps", type=Decimal, default=Decimal("50"))
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds between market scans")
    parser.add_argument("--include-expired", action="store_true", help="scan markets whose closesAt/closesBettingAt is in the past")
    parser.add_argument("--account", action="store_true", help="print authenticated balances/orders/trades and exit")
    parser.add_argument("--account-raw", action="store_true", help="with --account, include raw account records")
    parser.add_argument("--bitcoin-5m", action="store_true", help="scan native Previsao Bitcoin 5-minute markets against matching Polymarket BTC 5m markets")
    parser.add_argument("--debug-books", action="store_true", help="print best bid/ask snapshots while scanning")
    parser.add_argument("--maker-plan", action="store_true", help="for --bitcoin-5m, print Previsao maker quotes hedged by Polymarket")
    parser.add_argument("--maker-margin-pct", type=Decimal, default=Decimal("15"), help="margin applied to maker quotes, default 15")
    parser.add_argument("--max-order-usdc", type=Decimal, default=Decimal("2"), help="maximum USDC cost per Previsao maker quote")
    parser.add_argument("--min-seconds-left", type=int, default=90, help="skip maker quotes when less than this many seconds remain")
    parser.add_argument("--execute-previsao-maker", action="store_true", help="LIVE: create/cancel Previsao maker orders from --maker-plan")
    parser.add_argument("--disable-ws", action="store_true", help="disable websocket accelerators and use REST polling only")
    parser.add_argument("--event-debounce", type=float, default=float(os.getenv("BOT_EVENT_DEBOUNCE", "0.3")), help="seconds to debounce websocket-triggered requotes")
    parser.add_argument("--watch-interval", type=float, default=0.0, help="repeat scan every N seconds; 0 runs once")
    parser.add_argument("--iterations", type=int, default=0, help="stop after N watch iterations; 0 means forever when --watch-interval is set")
    parser.add_argument("--lock-path", default=os.getenv("BOT_LOCK_PATH", "work/bot.lock"), help="live trading process lock path")
    parser.add_argument("--stop-path", default=os.getenv("BOT_STOP_PATH", "work/STOP_BOT"), help="create this file to stop live trading safely")
    parser.add_argument("--dashboard", action="store_true", help="start local visual dashboard")
    parser.add_argument("--dashboard-host", default="127.0.0.1")
    parser.add_argument("--dashboard-port", type=int, default=8787)
    args = parser.parse_args()

    min_edge = args.min_edge_bps / Decimal("10000")
    previsao = PrevisaoClient(
        os.getenv("PREVISAO_API_BASE", "https://app.previsao.io/api/v1"),
        api_key=os.getenv("PREVISAO_API_KEY"),
        api_secret=os.getenv("PREVISAO_API_SECRET"),
    )
    poly = PolymarketClient(
        os.getenv("POLYMARKET_CLOB_BASE", "https://clob.polymarket.com"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
        address=os.getenv("POLYMARKET_ADDRESS"),
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        funder=os.getenv("POLYMARKET_FUNDER"),
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1")),
        chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
    )
    gamma = PolymarketGammaClient(os.getenv("POLYMARKET_GAMMA_BASE", "https://gamma-api.polymarket.com"))
    ws_enabled = not args.disable_ws and os.getenv("BOT_ENABLE_WS", "1") != "0"
    quote_refresh_event = threading.Event()
    previsao_public_ws: PrevisaoWsPublicMonitor | None = None
    previsao_ws: PrevisaoWsHedgeAccelerator | None = None
    polymarket_user_ws: PolymarketWsUserMonitor | None = None
    if ws_enabled:
        poly.book_cache = PolymarketWsBookCache(
            os.getenv("POLYMARKET_WS_MARKET_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            quote_refresh_event,
        )
        poly.book_cache.start()
        previsao_public_ws = PrevisaoWsPublicMonitor(previsao, quote_refresh_event)
        previsao_public_ws.start()
        if args.execute_previsao_maker:
            state_path = os.getenv("BOT_STATE_PATH", "work/hedge_state.json")
            previsao_ws = PrevisaoWsHedgeAccelerator(
                previsao,
                poly,
                state_path,
                quote_refresh_event,
            )
            previsao_ws.start()
            polymarket_user_ws = PolymarketWsUserMonitor(
                poly,
                state_path,
                os.getenv("POLYMARKET_WS_USER_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/user"),
            )
            polymarket_user_ws.start()

    if args.account:
        emit_account_snapshot(previsao, poly, raw=args.account_raw)
        return 0

    if args.dashboard:
        run_dashboard(previsao, gamma, poly, args.dashboard_host, args.dashboard_port)
        return 0

    if args.bitcoin_5m:
        iteration = 0
        guard = LiveTradeGuard(args.lock_path, args.stop_path)
        if args.execute_previsao_maker:
            guard.acquire()
        while True:
            iteration += 1
            if args.execute_previsao_maker:
                guard.check()
            maker_margin_pct, max_order_usdc, min_seconds_left, bot_enabled = load_remote_bot_config(
                args.maker_margin_pct,
                args.max_order_usdc,
                args.min_seconds_left,
            )
            if args.execute_previsao_maker and not bot_enabled:
                state_path = os.getenv("BOT_STATE_PATH", "work/hedge_state.json")
                state = enrich_hedged_trade_settlements(previsao, state_path)
                publish_dashboard_state(state, build_runtime_health(poly, previsao_public_ws, previsao_ws, polymarket_user_ws))
                result = cancel_all_previsao_maker_orders(previsao)
                print(
                    json.dumps(
                        {
                            "level": "paused",
                            "mode": "bitcoin_5m",
                            "iteration": iteration,
                            "sync": result,
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                if not args.watch_interval:
                    break
                if args.iterations and iteration >= args.iterations:
                    break
                time.sleep(args.watch_interval)
                continue
            try:
                if args.execute_previsao_maker:
                    state_path = os.getenv("BOT_STATE_PATH", "work/hedge_state.json")
                    state = enrich_hedged_trade_settlements(previsao, state_path)
                    breaker = enforce_loss_streak_breaker(previsao, state, state_path, args.stop_path)
                    publish_dashboard_state(state, build_runtime_health(poly, previsao_public_ws, previsao_ws, polymarket_user_ws))
                    if breaker is not None:
                        print(
                            json.dumps(
                                {
                                    "level": "stopped",
                                    "mode": "bitcoin_5m",
                                    "iteration": iteration,
                                    "reason": breaker.get("reason"),
                                    "trade_ids": breaker.get("trade_ids"),
                                    "sync": breaker.get("cancel"),
                                    "remote_config": breaker.get("remote_config"),
                                },
                                ensure_ascii=False,
                            ),
                            file=sys.stderr,
                        )
                        return 0
                scanned = scan_bitcoin_5m(
                    previsao,
                    gamma,
                    poly,
                    min_edge,
                    args.depth,
                    debug_books=args.debug_books,
                    maker_plan=args.maker_plan,
                    maker_margin_pct=maker_margin_pct,
                    max_order_usdc=max_order_usdc,
                    min_seconds_left=min_seconds_left,
                    execute_previsao_maker=args.execute_previsao_maker,
                )
            except Exception as exc:
                scanned = 0
                print(
                    json.dumps(
                        {
                            "level": "loop_error",
                            "mode": "bitcoin_5m",
                            "iteration": iteration,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
            print(json.dumps({"level": "summary", "scanned_markets": scanned, "mode": "bitcoin_5m", "iteration": iteration}), file=sys.stderr)
            if not args.watch_interval:
                break
            if args.iterations and iteration >= args.iterations:
                break
            if quote_refresh_event.wait(args.watch_interval):
                time.sleep(max(0.0, args.event_debounce))
                quote_refresh_event.clear()
        return 0

    scanned = 0
    for page in range(1, args.pages + 1):
        data = previsao.mirrored_markets(page=page, limit=args.per_page)
        for market in data.get("items", []):
            if market.get("status") != "OPEN":
                continue
            if not args.include_expired and is_expired(market):
                continue
            scanned += 1
            try:
                scan_market(market, previsao, poly, min_edge, args.depth)
            except Exception as exc:
                print(json.dumps({"level": "error", "market_id": market.get("id"), "error": str(exc)}), file=sys.stderr)
            if args.sleep:
                time.sleep(args.sleep)

    print(json.dumps({"level": "summary", "scanned_markets": scanned}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
