"""
GRVT-Variational Dual Exchange Arbitrage Bot
=============================================
GRVT에서 지정가 주문이 체결되면 Variational에서 반대 포지션을 자동으로 잡는 봇
포지션 유지 시간 후 자동 청산 기능 포함

사용법:
    python dual_arb_bot.py
"""

import asyncio
import sys

# Windows에서 aiodns SelectorEventLoop 문제 해결
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from dataclasses import dataclass, field
from typing import Optional, Callable
from datetime import datetime
import threading
import traceback
import time


@dataclass
class ArbitrageOrder:
    """차익거래 주문 정보"""
    coin: str
    side: str  # 'buy' or 'sell' (GRVT 주문 방향)
    amount: float
    price: float
    leverage: float = 1.0
    hold_minutes: float = 0
    close_price: Optional[float] = None
    grvt_order_id: Optional[str] = None
    variational_order_id: Optional[str] = None
    grvt_position_size: float = 0
    variational_position_size: float = 0
    status: str = "pending"


class ArbitrageBot:
    """GRVT-Variational 양방향 차익거래 봇"""

    def __init__(self, log_callback: Callable[[str], None] = None,
                 timer_callback: Callable[[int], None] = None,
                 status_callback: Callable[[str], None] = None):
        self.grvt = None
        self.variational = None
        self.running = False
        self.current_order: Optional[ArbitrageOrder] = None
        self.log_callback = log_callback or print
        self.timer_callback = timer_callback
        self.status_callback = status_callback
        self.poll_interval = 2.0

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_callback(f"[{timestamp}] {message}")

    def update_timer(self, remaining_seconds: int):
        if self.timer_callback:
            self.timer_callback(remaining_seconds)

    def update_status(self, status: str):
        if self.status_callback:
            self.status_callback(status)

    async def initialize(self):
        try:
            from mpdex import create_exchange, symbol_create

            try:
                from keys.pk_grvt import GRVT_KEY
            except ImportError:
                self.log("keys/pk_grvt.py 파일을 찾을 수 없습니다.")
                return False

            try:
                from keys.pk_variational import VARIATIONAL_KEY
            except ImportError:
                self.log("keys/pk_variational.py 파일을 찾을 수 없습니다.")
                return False

            self.log("GRVT 연결 중...")
            self.grvt = await create_exchange('grvt', GRVT_KEY)
            self.log("GRVT 연결 완료")

            self.log("Variational 연결 중...")
            self.variational = await create_exchange('variational', VARIATIONAL_KEY)
            self.log("Variational 연결 완료")

            return True

        except Exception as e:
            self.log(f"초기화 실패: {e}")
            traceback.print_exc()
            return False

    async def close(self):
        try:
            if self.grvt:
                await self.grvt.close()
            if self.variational:
                await self.variational.close()
            self.log("연결 종료됨")
        except Exception as e:
            self.log(f"종료 중 에러: {e}")

    def get_grvt_symbol(self, coin: str) -> str:
        return f"{coin.upper()}_USDT_Perp"

    def get_variational_symbol(self, coin: str) -> str:
        return coin.upper()

    def get_opposite_side(self, side: str) -> str:
        return 'sell' if side.lower() == 'buy' else 'buy'

    async def get_mark_price(self, coin: str) -> Optional[float]:
        try:
            symbol = self.get_grvt_symbol(coin)
            price = await self.grvt.get_mark_price(symbol)
            return float(price)
        except Exception as e:
            self.log(f"마크 가격 조회 실패: {e}")
            return None

    async def place_grvt_limit_order(self, coin: str, side: str, amount: float, price: float) -> Optional[str]:
        try:
            symbol = self.get_grvt_symbol(coin)
            self.log(f"GRVT 지정가 주문: {symbol} {side.upper()} {amount} @ {price}")

            order_id = await self.grvt.create_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount,
                price=price,
                order_type='limit'
            )

            self.log(f"GRVT 주문 완료 (ID: {order_id})")
            return order_id

        except Exception as e:
            self.log(f"GRVT 주문 실패: {e}")
            traceback.print_exc()
            return None

    async def check_grvt_order_filled(self, coin: str, order_id: str) -> bool:
        try:
            symbol = self.get_grvt_symbol(coin)
            open_orders = await self.grvt.get_open_orders(symbol)

            if open_orders is None:
                return True

            for order in open_orders:
                if str(order.get('id')) == str(order_id):
                    return False

            return True

        except Exception as e:
            self.log(f"GRVT 주문 확인 에러: {e}")
            return False

    async def place_variational_market_order(self, coin: str, side: str, amount: float) -> Optional[str]:
        try:
            symbol = self.get_variational_symbol(coin)
            self.log(f"Variational 시장가 주문: {symbol} {side.upper()} {amount}")

            order_id = await self.variational.create_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount,
                order_type='market'
            )

            self.log(f"Variational 주문 완료 (ID: {order_id})")
            return order_id

        except Exception as e:
            self.log(f"Variational 주문 실패: {e}")
            traceback.print_exc()
            return None

    async def get_positions(self, coin: str) -> dict:
        result = {"grvt": None, "variational": None}

        try:
            grvt_symbol = self.get_grvt_symbol(coin)
            result["grvt"] = await self.grvt.get_position(grvt_symbol)
        except Exception as e:
            self.log(f"GRVT 포지션 조회 실패: {e}")

        try:
            var_symbol = self.get_variational_symbol(coin)
            result["variational"] = await self.variational.get_position(var_symbol)
        except Exception as e:
            self.log(f"Variational 포지션 조회 실패: {e}")

        return result

    async def get_collaterals(self) -> dict:
        result = {"grvt": None, "variational": None}

        try:
            result["grvt"] = await self.grvt.get_collateral()
        except Exception as e:
            self.log(f"GRVT 담보금 조회 실패: {e}")

        try:
            result["variational"] = await self.variational.get_collateral()
        except Exception as e:
            self.log(f"Variational 담보금 조회 실패: {e}")

        return result

    async def verify_and_match_positions(self, coin: str, expected_amount: float, grvt_side: str) -> bool:
        self.log("포지션 수량 확인 중...")

        positions = await self.get_positions(coin)
        grvt_pos = positions.get("grvt")
        var_pos = positions.get("variational")

        grvt_size = 0.0
        if grvt_pos:
            grvt_size = float(grvt_pos.get('size', 0))
            self.current_order.grvt_position_size = grvt_size

        var_size = 0.0
        if var_pos:
            var_size = float(var_pos.get('size', 0))
            self.current_order.variational_position_size = var_size

        self.log(f"GRVT 포지션: {grvt_size}, Variational 포지션: {var_size}")

        diff = abs(grvt_size - var_size)
        tolerance = expected_amount * 0.01

        if diff > tolerance:
            self.log(f"포지션 불일치 발견: 차이 {diff}")

            if var_size < grvt_size:
                adjust_amount = grvt_size - var_size
                opposite_side = self.get_opposite_side(grvt_side)
                self.log(f"Variational 추가 주문: {opposite_side.upper()} {adjust_amount}")
                await self.place_variational_market_order(coin, opposite_side, adjust_amount)
            elif var_size > grvt_size:
                adjust_amount = var_size - grvt_size
                self.log(f"Variational 일부 청산: {grvt_side.upper()} {adjust_amount}")
                await self.place_variational_market_order(coin, grvt_side, adjust_amount)

            await asyncio.sleep(2)
            positions = await self.get_positions(coin)
            grvt_pos = positions.get("grvt")
            var_pos = positions.get("variational")

            grvt_size = float(grvt_pos.get('size', 0)) if grvt_pos else 0
            var_size = float(var_pos.get('size', 0)) if var_pos else 0

            self.current_order.grvt_position_size = grvt_size
            self.current_order.variational_position_size = var_size

            self.log(f"조정 후 - GRVT: {grvt_size}, Variational: {var_size}")

        self.log("포지션 수량 매칭 완료")
        return True

    async def close_positions(self, coin: str, grvt_side: str, amount: float, close_price: float):
        grvt_close_side = self.get_opposite_side(grvt_side)

        self.log(f"=== 포지션 청산 시작 ===")
        self.update_status("GRVT 청산 대기중")
        self.current_order.status = "closing_grvt"

        close_amount = self.current_order.grvt_position_size or amount
        grvt_close_order_id = await self.place_grvt_limit_order(
            coin, grvt_close_side, close_amount, close_price
        )

        if grvt_close_order_id is None:
            self.log("GRVT 청산 주문 실패")
            return False

        self.log(f"GRVT 청산 체결 대기 중 (폴링 간격: {self.poll_interval}초)")

        while self.running:
            is_filled = await self.check_grvt_order_filled(coin, grvt_close_order_id)

            if is_filled:
                self.log("GRVT 청산 체결됨!")
                break

            await asyncio.sleep(self.poll_interval)

        if not self.running:
            self.log("사용자에 의해 중지됨")
            return False

        self.update_status("Variational 청산중")
        self.current_order.status = "closing_variational"

        var_close_side = grvt_side
        var_close_amount = self.current_order.variational_position_size or amount

        self.log(f"Variational 청산: {var_close_side.upper()} {var_close_amount} (시장가)")

        var_close_order_id = await self.place_variational_market_order(
            coin, var_close_side, var_close_amount
        )

        if var_close_order_id:
            self.log("Variational 청산 완료!")
            return True
        else:
            self.log("Variational 청산 실패")
            return False

    async def start_arbitrage(self, coin: str, side: str, amount: float, price: float,
                               leverage: float = 1.0, hold_minutes: float = 0,
                               close_price: Optional[float] = None):
        if self.running:
            self.log("이미 실행 중입니다.")
            return

        self.running = True
        self.current_order = ArbitrageOrder(
            coin=coin,
            side=side,
            amount=amount,
            price=price,
            leverage=leverage,
            hold_minutes=hold_minutes,
            close_price=close_price
        )

        try:
            self.update_status("GRVT 주문 대기중")
            order_id = await self.place_grvt_limit_order(coin, side, amount, price)
            if order_id is None:
                self.current_order.status = "failed"
                self.running = False
                return

            self.current_order.grvt_order_id = order_id
            self.current_order.status = "grvt_placed"

            self.log(f"GRVT 주문 모니터링 시작 (폴링 간격: {self.poll_interval}초)")

            while self.running and self.current_order.status == "grvt_placed":
                is_filled = await self.check_grvt_order_filled(coin, order_id)

                if is_filled:
                    self.log("GRVT 주문 체결됨!")
                    self.current_order.status = "grvt_filled"
                    break

                await asyncio.sleep(self.poll_interval)

            if not self.running:
                self.log("사용자에 의해 중지됨")
                return

            if self.current_order.status == "grvt_filled":
                opposite_side = self.get_opposite_side(side)
                self.update_status("Variational 진입중")
                self.log(f"Variational 반대 포지션 진입: {opposite_side.upper()}")

                var_order_id = await self.place_variational_market_order(coin, opposite_side, amount)

                if var_order_id:
                    self.current_order.variational_order_id = var_order_id
                    self.current_order.status = "variational_filled"
                    self.log("양쪽 포지션 진입 완료!")
                else:
                    self.current_order.status = "failed"
                    self.log("Variational 주문 실패")
                    self.running = False
                    return

            await asyncio.sleep(2)
            await self.verify_and_match_positions(coin, amount, side)

            if hold_minutes > 0 and self.current_order.status == "variational_filled":
                self.current_order.status = "holding"
                total_seconds = int(hold_minutes * 60)
                self.log(f"=== 포지션 유지 시작: {hold_minutes}분 ({total_seconds}초) ===")

                end_time = time.time() + total_seconds

                while self.running and time.time() < end_time:
                    remaining = int(end_time - time.time())
                    self.update_timer(remaining)

                    mins, secs = divmod(remaining, 60)
                    self.update_status(f"포지션 유지중 {mins:02d}:{secs:02d}")

                    await asyncio.sleep(1)

                if not self.running:
                    self.log("사용자에 의해 중지됨")
                    return

                self.update_timer(0)
                self.log("유지 시간 종료!")

                if close_price is None:
                    mark_price = await self.get_mark_price(coin)
                    if mark_price:
                        if side == 'buy':
                            close_price = mark_price * 0.999
                        else:
                            close_price = mark_price * 1.001
                        self.log(f"청산 가격 자동 설정: {close_price:.2f}")
                    else:
                        close_price = price

                success = await self.close_positions(coin, side, amount, close_price)

                if success:
                    self.current_order.status = "completed"
                    self.update_status("차익거래 완료!")
                    self.log("=== 차익거래 완료! ===")
                else:
                    self.current_order.status = "failed"
                    self.update_status("청산 실패")

            else:
                self.current_order.status = "completed"
                self.update_status("진입 완료 (수동 청산 필요)")
                self.log("양쪽 포지션 진입 완료! (수동 청산 필요)")

        except Exception as e:
            self.log(f"차익거래 에러: {e}")
            traceback.print_exc()
            self.current_order.status = "failed"
        finally:
            self.running = False

    async def stop(self):
        if not self.running:
            return

        self.running = False
        self.log("중지 요청됨...")

        if self.current_order and self.current_order.status == "grvt_placed":
            try:
                symbol = self.get_grvt_symbol(self.current_order.coin)
                await self.grvt.cancel_orders(symbol)
                self.log("GRVT 주문 취소됨")
            except Exception as e:
                self.log(f"GRVT 주문 취소 실패: {e}")


class ArbitrageGUI:
    """차익거래 봇 GUI"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("GRVT-Variational 차익거래 봇")
        self.root.geometry("750x800")
        self.root.resizable(True, True)

        self.bot: Optional[ArbitrageBot] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.async_thread: Optional[threading.Thread] = None

        self._setup_ui()
        self._start_async_loop()

    def _setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # === 연결 상태 ===
        conn_frame = ttk.LabelFrame(main_frame, text="연결 상태", padding="5")
        conn_frame.pack(fill=tk.X, pady=(0, 10))

        self.conn_status = ttk.Label(conn_frame, text="연결 안됨", foreground="gray")
        self.conn_status.pack(side=tk.LEFT, padx=5)

        self.btn_connect = ttk.Button(conn_frame, text="연결", command=self._on_connect)
        self.btn_connect.pack(side=tk.RIGHT, padx=5)

        # === 주문 설정 ===
        order_frame = ttk.LabelFrame(main_frame, text="주문 설정 (GRVT 지정가 진입)", padding="10")
        order_frame.pack(fill=tk.X, pady=(0, 10))

        # 코인 & 방향
        row1 = ttk.Frame(order_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="코인:", width=10).pack(side=tk.LEFT)
        self.coin_var = tk.StringVar(value="BTC")
        coin_combo = ttk.Combobox(row1, textvariable=self.coin_var,
                                   values=["BTC", "ETH", "SOL", "ARB", "DOGE", "XRP", "LINK", "AVAX"],
                                   width=12)
        coin_combo.pack(side=tk.LEFT, padx=5)

        ttk.Label(row1, text="방향:", width=8).pack(side=tk.LEFT, padx=(10, 0))
        self.side_var = tk.StringVar(value="buy")
        side_frame = ttk.Frame(row1)
        side_frame.pack(side=tk.LEFT)
        ttk.Radiobutton(side_frame, text="롱(Buy)", variable=self.side_var, value="buy").pack(side=tk.LEFT)
        ttk.Radiobutton(side_frame, text="숏(Sell)", variable=self.side_var, value="sell").pack(side=tk.LEFT, padx=5)

        # 진입가격 & 레버리지
        row2 = ttk.Frame(order_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="진입가격:", width=10).pack(side=tk.LEFT)
        self.price_var = tk.StringVar(value="95000")
        ttk.Entry(row2, textvariable=self.price_var, width=15).pack(side=tk.LEFT, padx=5)

        ttk.Label(row2, text="레버리지:", width=10).pack(side=tk.LEFT, padx=(10, 0))
        self.leverage_var = tk.StringVar(value="10")
        ttk.Entry(row2, textvariable=self.leverage_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text="x").pack(side=tk.LEFT)

        # 수량 입력 방식 선택
        row3 = ttk.Frame(order_frame)
        row3.pack(fill=tk.X, pady=5)

        self.input_mode_var = tk.StringVar(value="qty")
        ttk.Radiobutton(row3, text="수량으로 입력", variable=self.input_mode_var,
                        value="qty", command=self._on_input_mode_change).pack(side=tk.LEFT)
        ttk.Radiobutton(row3, text="USDC로 입력", variable=self.input_mode_var,
                        value="usdc", command=self._on_input_mode_change).pack(side=tk.LEFT, padx=10)

        # 수량/USDC 입력
        row4 = ttk.Frame(order_frame)
        row4.pack(fill=tk.X, pady=2)

        self.qty_label = ttk.Label(row4, text="수량:", width=10)
        self.qty_label.pack(side=tk.LEFT)
        self.amount_var = tk.StringVar(value="0.001")
        self.amount_entry = ttk.Entry(row4, textvariable=self.amount_var, width=15)
        self.amount_entry.pack(side=tk.LEFT, padx=5)

        self.usdc_label = ttk.Label(row4, text="USDC:", width=10)
        self.usdc_var = tk.StringVar(value="100")
        self.usdc_entry = ttk.Entry(row4, textvariable=self.usdc_var, width=15)

        # 계산된 수량 표시
        self.calc_label = ttk.Label(row4, text="", foreground="blue")
        self.calc_label.pack(side=tk.LEFT, padx=10)

        # USDC 입력 시 수량 자동 계산
        self.usdc_var.trace_add("write", self._on_usdc_change)
        self.price_var.trace_add("write", self._on_usdc_change)
        self.leverage_var.trace_add("write", self._on_usdc_change)

        # === 유지 시간 & 청산 설정 ===
        hold_frame = ttk.LabelFrame(main_frame, text="포지션 유지 & 청산 설정", padding="10")
        hold_frame.pack(fill=tk.X, pady=(0, 10))

        row5 = ttk.Frame(hold_frame)
        row5.pack(fill=tk.X, pady=2)
        ttk.Label(row5, text="유지 시간:", width=10).pack(side=tk.LEFT)
        self.hold_var = tk.StringVar(value="10")
        ttk.Entry(row5, textvariable=self.hold_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Label(row5, text="분 (0=자동청산 안함)").pack(side=tk.LEFT)

        ttk.Label(row5, text="청산가격:", width=10).pack(side=tk.LEFT, padx=(20, 0))
        self.close_price_var = tk.StringVar(value="")
        ttk.Entry(row5, textvariable=self.close_price_var, width=15).pack(side=tk.LEFT, padx=5)
        ttk.Label(row5, text="(빈칸=자동)").pack(side=tk.LEFT)

        row6 = ttk.Frame(hold_frame)
        row6.pack(fill=tk.X, pady=2)
        ttk.Label(row6, text="폴링 간격:", width=10).pack(side=tk.LEFT)
        self.poll_var = tk.StringVar(value="2.0")
        ttk.Entry(row6, textvariable=self.poll_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Label(row6, text="초").pack(side=tk.LEFT)

        # === 실행 버튼 ===
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        self.btn_start = ttk.Button(btn_frame, text="차익거래 시작", command=self._on_start, state=tk.DISABLED)
        self.btn_start.pack(side=tk.LEFT, padx=5)

        self.btn_stop = ttk.Button(btn_frame, text="중지", command=self._on_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        self.btn_refresh = ttk.Button(btn_frame, text="포지션 조회", command=self._on_refresh, state=tk.DISABLED)
        self.btn_refresh.pack(side=tk.RIGHT, padx=5)

        self.btn_calc = ttk.Button(btn_frame, text="수량 계산", command=self._on_calc)
        self.btn_calc.pack(side=tk.RIGHT, padx=5)

        # === 상태 & 타이머 ===
        status_frame = ttk.LabelFrame(main_frame, text="현재 상태", padding="5")
        status_frame.pack(fill=tk.X, pady=(0, 10))

        status_row = ttk.Frame(status_frame)
        status_row.pack(fill=tk.X)

        self.status_label = ttk.Label(status_row, text="대기 중", font=("", 11, "bold"))
        self.status_label.pack(side=tk.LEFT)

        self.timer_label = ttk.Label(status_row, text="", font=("", 14, "bold"), foreground="blue")
        self.timer_label.pack(side=tk.RIGHT, padx=10)

        # === 포지션 정보 ===
        pos_frame = ttk.LabelFrame(main_frame, text="포지션 정보", padding="5")
        pos_frame.pack(fill=tk.X, pady=(0, 10))

        grvt_row = ttk.Frame(pos_frame)
        grvt_row.pack(fill=tk.X, pady=2)
        ttk.Label(grvt_row, text="GRVT:", width=12, font=("", 10, "bold")).pack(side=tk.LEFT)
        self.grvt_pos_label = ttk.Label(grvt_row, text="-", foreground="gray")
        self.grvt_pos_label.pack(side=tk.LEFT)

        var_row = ttk.Frame(pos_frame)
        var_row.pack(fill=tk.X, pady=2)
        ttk.Label(var_row, text="Variational:", width=12, font=("", 10, "bold")).pack(side=tk.LEFT)
        self.var_pos_label = ttk.Label(var_row, text="-", foreground="gray")
        self.var_pos_label.pack(side=tk.LEFT)

        # === 담보금 정보 ===
        coll_frame = ttk.LabelFrame(main_frame, text="담보금", padding="5")
        coll_frame.pack(fill=tk.X, pady=(0, 10))

        coll_row = ttk.Frame(coll_frame)
        coll_row.pack(fill=tk.X)
        ttk.Label(coll_row, text="GRVT:", width=12).pack(side=tk.LEFT)
        self.grvt_coll_label = ttk.Label(coll_row, text="-")
        self.grvt_coll_label.pack(side=tk.LEFT)
        ttk.Label(coll_row, text="  |  Variational:", width=15).pack(side=tk.LEFT)
        self.var_coll_label = ttk.Label(coll_row, text="-")
        self.var_coll_label.pack(side=tk.LEFT)

        # === 로그 ===
        log_frame = ttk.LabelFrame(main_frame, text="로그", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state=tk.DISABLED,
                                                   font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 설명
        desc_text = "GRVT 지정가 체결 -> Variational 반대 포지션 -> 유지 시간 후 자동 청산"
        ttk.Label(main_frame, text=desc_text, foreground="gray", font=("", 9)).pack(pady=(5, 0))

    def _on_input_mode_change(self):
        """입력 방식 변경 시 UI 업데이트"""
        mode = self.input_mode_var.get()
        if mode == "qty":
            self.qty_label.pack(side=tk.LEFT)
            self.amount_entry.pack(side=tk.LEFT, padx=5)
            self.usdc_label.pack_forget()
            self.usdc_entry.pack_forget()
            self.calc_label.config(text="")
        else:
            self.qty_label.pack_forget()
            self.amount_entry.pack_forget()
            self.usdc_label.pack(side=tk.LEFT)
            self.usdc_entry.pack(side=tk.LEFT, padx=5)
            self._calculate_quantity()

    def _on_usdc_change(self, *args):
        """USDC, 가격, 레버리지 변경 시 수량 계산"""
        if self.input_mode_var.get() == "usdc":
            self._calculate_quantity()

    def _calculate_quantity(self) -> Optional[float]:
        """USDC와 레버리지로 수량 계산"""
        try:
            usdc = float(self.usdc_var.get())
            price = float(self.price_var.get())
            leverage = float(self.leverage_var.get())

            if price <= 0 or leverage <= 0:
                self.calc_label.config(text="")
                return None

            # 포지션 가치 = USDC × 레버리지
            # 수량 = 포지션 가치 / 가격
            position_value = usdc * leverage
            quantity = position_value / price

            self.calc_label.config(text=f"= {quantity:.6f} 개")
            return quantity

        except ValueError:
            self.calc_label.config(text="")
            return None

    def _on_calc(self):
        """수량 계산 버튼 클릭"""
        qty = self._calculate_quantity()
        if qty:
            self.log(f"계산된 수량: {qty:.6f}")
            if self.input_mode_var.get() == "usdc":
                usdc = self.usdc_var.get()
                leverage = self.leverage_var.get()
                price = self.price_var.get()
                self.log(f"  USDC: {usdc} × 레버리지: {leverage}x / 가격: {price} = {qty:.6f}")

    def _start_async_loop(self):
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        self.async_thread = threading.Thread(target=run_loop, daemon=True)
        self.async_thread.start()

    def _run_async(self, coro):
        if self.loop:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None

    def log(self, message: str):
        def _update():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _update)

    def _update_status(self, text: str, color: str = "black"):
        def _update():
            self.status_label.config(text=text, foreground=color)
        self.root.after(0, _update)

    def _update_timer(self, remaining_seconds: int):
        def _update():
            if remaining_seconds > 0:
                mins, secs = divmod(remaining_seconds, 60)
                self.timer_label.config(text=f"{mins:02d}:{secs:02d}")
            else:
                self.timer_label.config(text="")
        self.root.after(0, _update)

    def _on_connect(self):
        self.btn_connect.config(state=tk.DISABLED)
        self.conn_status.config(text="연결 중...", foreground="blue")
        self.log("거래소 연결 중...")

        async def connect():
            self.bot = ArbitrageBot(
                log_callback=self.log,
                timer_callback=self._update_timer,
                status_callback=lambda s: self._update_status(s, "blue")
            )
            success = await self.bot.initialize()

            def update_ui():
                if success:
                    self.conn_status.config(text="연결됨", foreground="green")
                    self.btn_start.config(state=tk.NORMAL)
                    self.btn_refresh.config(state=tk.NORMAL)
                    self._update_status("준비 완료", "green")
                else:
                    self.conn_status.config(text="연결 실패", foreground="red")
                    self.btn_connect.config(state=tk.NORMAL)
                    self._update_status("연결 실패", "red")

            self.root.after(0, update_ui)

        self._run_async(connect())

    def _on_start(self):
        try:
            coin = self.coin_var.get().strip().upper()
            side = self.side_var.get()
            price = float(self.price_var.get())
            leverage = float(self.leverage_var.get())
            hold_minutes = float(self.hold_var.get())
            poll_interval = float(self.poll_var.get())

            # 수량 결정
            if self.input_mode_var.get() == "qty":
                amount = float(self.amount_var.get())
            else:
                # USDC로 수량 계산
                usdc = float(self.usdc_var.get())
                position_value = usdc * leverage
                amount = position_value / price
                self.log(f"USDC {usdc} × {leverage}x = 포지션 ${position_value:.2f} = {amount:.6f} {coin}")

            close_price_str = self.close_price_var.get().strip()
            close_price = float(close_price_str) if close_price_str else None

            if amount <= 0:
                messagebox.showerror("오류", "수량은 0보다 커야 합니다.")
                return
            if price <= 0:
                messagebox.showerror("오류", "가격은 0보다 커야 합니다.")
                return
            if leverage <= 0:
                messagebox.showerror("오류", "레버리지는 0보다 커야 합니다.")
                return

        except ValueError:
            messagebox.showerror("오류", "숫자를 올바르게 입력하세요.")
            return

        self.bot.poll_interval = poll_interval
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

        opposite = "숏(Sell)" if side == "buy" else "롱(Buy)"
        self._update_status(f"실행 중: GRVT {side.upper()} -> Variational {opposite}", "blue")

        self.log(f"=== 차익거래 시작 ===")
        self.log(f"GRVT 진입: {coin} {side.upper()} {amount:.6f} @ {price} (레버리지: {leverage}x)")
        self.log(f"Variational 진입: {coin} {opposite} {amount:.6f} (시장가)")
        if hold_minutes > 0:
            self.log(f"유지 시간: {hold_minutes}분")

        async def run():
            await self.bot.start_arbitrage(coin, side, amount, price, leverage, hold_minutes, close_price)

            def update_ui():
                self.btn_start.config(state=tk.NORMAL)
                self.btn_stop.config(state=tk.DISABLED)
                self._update_timer(0)

                if self.bot.current_order:
                    status = self.bot.current_order.status
                    if status == "completed":
                        self._update_status("완료!", "green")
                    elif status == "failed":
                        self._update_status("실패", "red")
                    else:
                        self._update_status("대기 중", "gray")

            self.root.after(0, update_ui)

        self._run_async(run())

    def _on_stop(self):
        if self.bot:
            self._run_async(self.bot.stop())
        self.btn_stop.config(state=tk.DISABLED)
        self._update_status("중지됨", "orange")
        self._update_timer(0)

    def _on_refresh(self):
        coin = self.coin_var.get().strip().upper()
        self.log(f"{coin} 포지션 및 담보금 조회 중...")

        async def refresh():
            positions = await self.bot.get_positions(coin)
            collaterals = await self.bot.get_collaterals()

            def update_ui():
                grvt_pos = positions.get("grvt")
                if grvt_pos:
                    side = grvt_pos.get('side', '-')
                    size = grvt_pos.get('size', '-')
                    entry = grvt_pos.get('entry_price', '-')
                    color = "green" if side == "long" else "red" if side == "short" else "gray"
                    self.grvt_pos_label.config(text=f"{side.upper()} {size} @ {entry}", foreground=color)
                else:
                    self.grvt_pos_label.config(text="포지션 없음", foreground="gray")

                var_pos = positions.get("variational")
                if var_pos:
                    side = var_pos.get('side', '-')
                    size = var_pos.get('size', '-')
                    entry = var_pos.get('avg_entry_price', '-')
                    color = "green" if side == "long" else "red" if side == "short" else "gray"
                    self.var_pos_label.config(text=f"{side.upper()} {size} @ {entry}", foreground=color)
                else:
                    self.var_pos_label.config(text="포지션 없음", foreground="gray")

                grvt_coll = collaterals.get("grvt")
                if grvt_coll:
                    total = grvt_coll.get('total_collateral', '-')
                    avail = grvt_coll.get('available_collateral', '-')
                    self.grvt_coll_label.config(text=f"${total} (가용: ${avail})")
                else:
                    self.grvt_coll_label.config(text="-")

                var_coll = collaterals.get("variational")
                if var_coll:
                    total = var_coll.get('total_collateral', '-')
                    avail = var_coll.get('available_collateral', '-')
                    self.var_coll_label.config(text=f"${total} (가용: ${avail})")
                else:
                    self.var_coll_label.config(text="-")

                self.log("조회 완료")

            self.root.after(0, update_ui)

        self._run_async(refresh())

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self.bot and self.bot.running:
            if not messagebox.askyesno("확인", "차익거래가 실행 중입니다. 종료하시겠습니까?"):
                return

        if self.bot:
            self._run_async(self.bot.close())

        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

        self.root.destroy()


def main():
    import os

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    sys.path.insert(0, script_dir)

    print("=" * 50)
    print("GRVT-Variational 차익거래 봇")
    print("=" * 50)
    print()
    print("GUI를 시작합니다...")
    print()

    app = ArbitrageGUI()
    app.run()


if __name__ == "__main__":
    main()
