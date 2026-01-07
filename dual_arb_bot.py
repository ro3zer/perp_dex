"""
GRVT-Variational Dual Exchange Arbitrage Bot
=============================================
GRVT에서 지정가 주문이 체결되면 Variational에서 반대 포지션을 자동으로 잡는 봇

사용법:
    python dual_arb_bot.py
"""

import asyncio
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from dataclasses import dataclass
from typing import Optional, Callable
from datetime import datetime
import threading
import traceback


@dataclass
class ArbitrageOrder:
    """차익거래 주문 정보"""
    coin: str
    side: str  # 'buy' or 'sell' (GRVT 주문 방향)
    amount: float
    price: float
    grvt_order_id: Optional[str] = None
    variational_order_id: Optional[str] = None
    status: str = "pending"  # pending, grvt_placed, grvt_filled, variational_placed, completed, failed


class ArbitrageBot:
    """GRVT-Variational 양방향 차익거래 봇"""

    def __init__(self, log_callback: Callable[[str], None] = None):
        self.grvt = None
        self.variational = None
        self.running = False
        self.current_order: Optional[ArbitrageOrder] = None
        self.log_callback = log_callback or print
        self.poll_interval = 2.0  # 폴링 간격 (초)

    def log(self, message: str):
        """로그 메시지 출력"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_callback(f"[{timestamp}] {message}")

    async def initialize(self):
        """거래소 초기화"""
        try:
            from mpdex import create_exchange, symbol_create

            # 키 파일 로드
            try:
                from keys.pk_grvt import GRVT_KEY
            except ImportError:
                self.log("❌ keys/pk_grvt.py 파일을 찾을 수 없습니다.")
                self.log("   keys/copy.pk_grvt.py를 pk_grvt.py로 복사하고 키를 입력하세요.")
                return False

            try:
                from keys.pk_variational import VARIATIONAL_KEY
            except ImportError:
                self.log("❌ keys/pk_variational.py 파일을 찾을 수 없습니다.")
                self.log("   keys/copy.pk_variational.py를 pk_variational.py로 복사하고 키를 입력하세요.")
                return False

            self.log("GRVT 연결 중...")
            self.grvt = await create_exchange('grvt', GRVT_KEY)
            self.log("✓ GRVT 연결 완료")

            self.log("Variational 연결 중...")
            self.variational = await create_exchange('variational', VARIATIONAL_KEY)
            self.log("✓ Variational 연결 완료")

            return True

        except Exception as e:
            self.log(f"❌ 초기화 실패: {e}")
            traceback.print_exc()
            return False

    async def close(self):
        """연결 종료"""
        try:
            if self.grvt:
                await self.grvt.close()
            if self.variational:
                await self.variational.close()
            self.log("연결 종료됨")
        except Exception as e:
            self.log(f"종료 중 에러: {e}")

    def get_grvt_symbol(self, coin: str) -> str:
        """GRVT 심볼 포맷"""
        return f"{coin.upper()}_USDT_Perp"

    def get_variational_symbol(self, coin: str) -> str:
        """Variational 심볼 포맷"""
        return coin.upper()

    def get_opposite_side(self, side: str) -> str:
        """반대 방향 반환"""
        return 'sell' if side.lower() == 'buy' else 'buy'

    async def place_grvt_limit_order(self, coin: str, side: str, amount: float, price: float) -> Optional[str]:
        """GRVT에 지정가 주문"""
        try:
            symbol = self.get_grvt_symbol(coin)
            self.log(f"GRVT 지정가 주문 중: {symbol} {side.upper()} {amount} @ {price}")

            order_id = await self.grvt.create_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount,
                price=price,
                order_type='limit'
            )

            self.log(f"✓ GRVT 주문 완료 (ID: {order_id})")
            return order_id

        except Exception as e:
            self.log(f"❌ GRVT 주문 실패: {e}")
            traceback.print_exc()
            return None

    async def check_grvt_order_filled(self, coin: str, order_id: str) -> bool:
        """GRVT 주문 체결 여부 확인"""
        try:
            symbol = self.get_grvt_symbol(coin)
            open_orders = await self.grvt.get_open_orders(symbol)

            if open_orders is None:
                return True  # 오픈 오더가 없으면 체결됨

            # order_id가 오픈 오더 리스트에 있는지 확인
            for order in open_orders:
                if str(order.get('id')) == str(order_id):
                    return False  # 아직 미체결

            return True  # 리스트에 없으면 체결됨

        except Exception as e:
            self.log(f"⚠ GRVT 주문 확인 에러: {e}")
            return False

    async def place_variational_market_order(self, coin: str, side: str, amount: float) -> Optional[str]:
        """Variational에 시장가 주문"""
        try:
            symbol = self.get_variational_symbol(coin)
            self.log(f"Variational 시장가 주문 중: {symbol} {side.upper()} {amount}")

            order_id = await self.variational.create_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount,
                order_type='market'
            )

            self.log(f"✓ Variational 주문 완료 (ID: {order_id})")
            return order_id

        except Exception as e:
            self.log(f"❌ Variational 주문 실패: {e}")
            traceback.print_exc()
            return None

    async def start_arbitrage(self, coin: str, side: str, amount: float, price: float):
        """차익거래 시작"""
        if self.running:
            self.log("⚠ 이미 실행 중입니다.")
            return

        self.running = True
        self.current_order = ArbitrageOrder(
            coin=coin,
            side=side,
            amount=amount,
            price=price
        )

        try:
            # 1. GRVT에 지정가 주문
            order_id = await self.place_grvt_limit_order(coin, side, amount, price)
            if order_id is None:
                self.current_order.status = "failed"
                self.running = False
                return

            self.current_order.grvt_order_id = order_id
            self.current_order.status = "grvt_placed"

            self.log(f"📊 GRVT 주문 모니터링 시작 (폴링 간격: {self.poll_interval}초)")

            # 2. 체결 대기 및 모니터링
            while self.running and self.current_order.status == "grvt_placed":
                is_filled = await self.check_grvt_order_filled(coin, order_id)

                if is_filled:
                    self.log("🎯 GRVT 주문 체결됨!")
                    self.current_order.status = "grvt_filled"
                    break

                await asyncio.sleep(self.poll_interval)

            if not self.running:
                self.log("⏹ 사용자에 의해 중지됨")
                return

            # 3. Variational에 반대 포지션 시장가 주문
            if self.current_order.status == "grvt_filled":
                opposite_side = self.get_opposite_side(side)
                self.log(f"🔄 Variational에서 반대 포지션 진입: {opposite_side.upper()}")

                var_order_id = await self.place_variational_market_order(coin, opposite_side, amount)

                if var_order_id:
                    self.current_order.variational_order_id = var_order_id
                    self.current_order.status = "completed"
                    self.log("✅ 차익거래 완료!")
                else:
                    self.current_order.status = "failed"
                    self.log("❌ Variational 주문 실패")

        except Exception as e:
            self.log(f"❌ 차익거래 에러: {e}")
            traceback.print_exc()
            self.current_order.status = "failed"
        finally:
            self.running = False

    async def stop(self):
        """차익거래 중지"""
        if not self.running:
            return

        self.running = False
        self.log("⏹ 중지 요청됨...")

        # GRVT 주문 취소
        if self.current_order and self.current_order.status == "grvt_placed":
            try:
                symbol = self.get_grvt_symbol(self.current_order.coin)
                await self.grvt.cancel_orders(symbol)
                self.log("✓ GRVT 주문 취소됨")
            except Exception as e:
                self.log(f"⚠ GRVT 주문 취소 실패: {e}")

    async def get_positions(self, coin: str) -> dict:
        """양쪽 거래소 포지션 조회"""
        result = {"grvt": None, "variational": None}

        try:
            grvt_symbol = self.get_grvt_symbol(coin)
            result["grvt"] = await self.grvt.get_position(grvt_symbol)
        except Exception as e:
            self.log(f"⚠ GRVT 포지션 조회 실패: {e}")

        try:
            var_symbol = self.get_variational_symbol(coin)
            result["variational"] = await self.variational.get_position(var_symbol)
        except Exception as e:
            self.log(f"⚠ Variational 포지션 조회 실패: {e}")

        return result

    async def get_collaterals(self) -> dict:
        """양쪽 거래소 담보금 조회"""
        result = {"grvt": None, "variational": None}

        try:
            result["grvt"] = await self.grvt.get_collateral()
        except Exception as e:
            self.log(f"⚠ GRVT 담보금 조회 실패: {e}")

        try:
            result["variational"] = await self.variational.get_collateral()
        except Exception as e:
            self.log(f"⚠ Variational 담보금 조회 실패: {e}")

        return result


class ArbitrageGUI:
    """차익거래 봇 GUI"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("GRVT-Variational 차익거래 봇")
        self.root.geometry("700x650")
        self.root.resizable(True, True)

        self.bot: Optional[ArbitrageBot] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.async_thread: Optional[threading.Thread] = None

        self._setup_ui()
        self._start_async_loop()

    def _setup_ui(self):
        """UI 구성"""
        # 메인 프레임
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # === 연결 상태 프레임 ===
        conn_frame = ttk.LabelFrame(main_frame, text="연결 상태", padding="5")
        conn_frame.pack(fill=tk.X, pady=(0, 10))

        self.conn_status = ttk.Label(conn_frame, text="⚪ 연결 안됨", foreground="gray")
        self.conn_status.pack(side=tk.LEFT, padx=5)

        self.btn_connect = ttk.Button(conn_frame, text="연결", command=self._on_connect)
        self.btn_connect.pack(side=tk.RIGHT, padx=5)

        # === 주문 설정 프레임 ===
        order_frame = ttk.LabelFrame(main_frame, text="주문 설정 (GRVT 지정가)", padding="10")
        order_frame.pack(fill=tk.X, pady=(0, 10))

        # 코인 선택
        row1 = ttk.Frame(order_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="코인:", width=10).pack(side=tk.LEFT)
        self.coin_var = tk.StringVar(value="BTC")
        coin_combo = ttk.Combobox(row1, textvariable=self.coin_var,
                                   values=["BTC", "ETH", "SOL", "ARB", "DOGE", "XRP", "LINK", "AVAX"],
                                   width=15)
        coin_combo.pack(side=tk.LEFT, padx=5)

        # 방향 선택
        ttk.Label(row1, text="방향:", width=10).pack(side=tk.LEFT, padx=(20, 0))
        self.side_var = tk.StringVar(value="buy")
        side_frame = ttk.Frame(row1)
        side_frame.pack(side=tk.LEFT)
        ttk.Radiobutton(side_frame, text="롱(Buy)", variable=self.side_var, value="buy").pack(side=tk.LEFT)
        ttk.Radiobutton(side_frame, text="숏(Sell)", variable=self.side_var, value="sell").pack(side=tk.LEFT, padx=10)

        # 수량 입력
        row2 = ttk.Frame(order_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="수량:", width=10).pack(side=tk.LEFT)
        self.amount_var = tk.StringVar(value="0.001")
        ttk.Entry(row2, textvariable=self.amount_var, width=18).pack(side=tk.LEFT, padx=5)

        # 가격 입력
        ttk.Label(row2, text="가격:", width=10).pack(side=tk.LEFT, padx=(20, 0))
        self.price_var = tk.StringVar(value="95000")
        ttk.Entry(row2, textvariable=self.price_var, width=18).pack(side=tk.LEFT, padx=5)

        # 폴링 간격
        row3 = ttk.Frame(order_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="폴링 간격:", width=10).pack(side=tk.LEFT)
        self.poll_var = tk.StringVar(value="2.0")
        ttk.Entry(row3, textvariable=self.poll_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Label(row3, text="초").pack(side=tk.LEFT)

        # === 실행 버튼 프레임 ===
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        self.btn_start = ttk.Button(btn_frame, text="▶ 차익거래 시작", command=self._on_start, state=tk.DISABLED)
        self.btn_start.pack(side=tk.LEFT, padx=5)

        self.btn_stop = ttk.Button(btn_frame, text="⏹ 중지", command=self._on_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        self.btn_refresh = ttk.Button(btn_frame, text="🔄 포지션 조회", command=self._on_refresh, state=tk.DISABLED)
        self.btn_refresh.pack(side=tk.RIGHT, padx=5)

        # === 상태 표시 프레임 ===
        status_frame = ttk.LabelFrame(main_frame, text="현재 상태", padding="5")
        status_frame.pack(fill=tk.X, pady=(0, 10))

        self.status_label = ttk.Label(status_frame, text="대기 중", font=("", 11, "bold"))
        self.status_label.pack(anchor=tk.W)

        # === 포지션 정보 프레임 ===
        pos_frame = ttk.LabelFrame(main_frame, text="포지션 정보", padding="5")
        pos_frame.pack(fill=tk.X, pady=(0, 10))

        # GRVT 포지션
        grvt_row = ttk.Frame(pos_frame)
        grvt_row.pack(fill=tk.X, pady=2)
        ttk.Label(grvt_row, text="GRVT:", width=12, font=("", 10, "bold")).pack(side=tk.LEFT)
        self.grvt_pos_label = ttk.Label(grvt_row, text="-", foreground="gray")
        self.grvt_pos_label.pack(side=tk.LEFT)

        # Variational 포지션
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

        # === 로그 프레임 ===
        log_frame = ttk.LabelFrame(main_frame, text="로그", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state=tk.DISABLED,
                                                   font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 설명 라벨
        desc_label = ttk.Label(main_frame,
                               text="※ GRVT에서 지정가 주문이 체결되면 Variational에서 자동으로 반대 포지션이 잡힙니다.",
                               foreground="gray", font=("", 9))
        desc_label.pack(pady=(5, 0))

    def _start_async_loop(self):
        """비동기 이벤트 루프 스레드 시작"""
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        self.async_thread = threading.Thread(target=run_loop, daemon=True)
        self.async_thread.start()

    def _run_async(self, coro):
        """코루틴을 비동기 스레드에서 실행"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None

    def log(self, message: str):
        """로그 추가 (스레드 안전)"""
        def _update():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _update)

    def _update_status(self, text: str, color: str = "black"):
        """상태 라벨 업데이트"""
        def _update():
            self.status_label.config(text=text, foreground=color)
        self.root.after(0, _update)

    def _on_connect(self):
        """연결 버튼 클릭"""
        self.btn_connect.config(state=tk.DISABLED)
        self.conn_status.config(text="🔵 연결 중...", foreground="blue")
        self.log("거래소 연결 중...")

        async def connect():
            self.bot = ArbitrageBot(log_callback=self.log)
            success = await self.bot.initialize()

            def update_ui():
                if success:
                    self.conn_status.config(text="🟢 연결됨", foreground="green")
                    self.btn_start.config(state=tk.NORMAL)
                    self.btn_refresh.config(state=tk.NORMAL)
                    self._update_status("준비 완료", "green")
                else:
                    self.conn_status.config(text="🔴 연결 실패", foreground="red")
                    self.btn_connect.config(state=tk.NORMAL)
                    self._update_status("연결 실패", "red")

            self.root.after(0, update_ui)

        self._run_async(connect())

    def _on_start(self):
        """시작 버튼 클릭"""
        try:
            coin = self.coin_var.get().strip().upper()
            side = self.side_var.get()
            amount = float(self.amount_var.get())
            price = float(self.price_var.get())
            poll_interval = float(self.poll_var.get())

            if amount <= 0:
                messagebox.showerror("오류", "수량은 0보다 커야 합니다.")
                return
            if price <= 0:
                messagebox.showerror("오류", "가격은 0보다 커야 합니다.")
                return

        except ValueError:
            messagebox.showerror("오류", "수량과 가격은 숫자여야 합니다.")
            return

        self.bot.poll_interval = poll_interval
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

        opposite = "숏(Sell)" if side == "buy" else "롱(Buy)"
        self._update_status(f"실행 중: GRVT {side.upper()} → Variational {opposite}", "blue")
        self.log(f"=== 차익거래 시작 ===")
        self.log(f"GRVT: {coin} {side.upper()} {amount} @ {price}")
        self.log(f"체결 시 Variational: {coin} {opposite} {amount} (시장가)")

        async def run():
            await self.bot.start_arbitrage(coin, side, amount, price)

            def update_ui():
                self.btn_start.config(state=tk.NORMAL)
                self.btn_stop.config(state=tk.DISABLED)
                if self.bot.current_order:
                    status = self.bot.current_order.status
                    if status == "completed":
                        self._update_status("✅ 차익거래 완료!", "green")
                    elif status == "failed":
                        self._update_status("❌ 실패", "red")
                    else:
                        self._update_status("대기 중", "gray")

            self.root.after(0, update_ui)

        self._run_async(run())

    def _on_stop(self):
        """중지 버튼 클릭"""
        if self.bot:
            self._run_async(self.bot.stop())
        self.btn_stop.config(state=tk.DISABLED)
        self._update_status("중지됨", "orange")

    def _on_refresh(self):
        """포지션 조회 버튼 클릭"""
        coin = self.coin_var.get().strip().upper()
        self.log(f"📊 {coin} 포지션 및 담보금 조회 중...")

        async def refresh():
            # 포지션 조회
            positions = await self.bot.get_positions(coin)
            collaterals = await self.bot.get_collaterals()

            def update_ui():
                # GRVT 포지션
                grvt_pos = positions.get("grvt")
                if grvt_pos:
                    side = grvt_pos.get('side', '-')
                    size = grvt_pos.get('size', '-')
                    entry = grvt_pos.get('entry_price', '-')
                    color = "green" if side == "long" else "red" if side == "short" else "gray"
                    self.grvt_pos_label.config(
                        text=f"{side.upper()} {size} @ {entry}",
                        foreground=color
                    )
                else:
                    self.grvt_pos_label.config(text="포지션 없음", foreground="gray")

                # Variational 포지션
                var_pos = positions.get("variational")
                if var_pos:
                    side = var_pos.get('side', '-')
                    size = var_pos.get('size', '-')
                    entry = var_pos.get('avg_entry_price', '-')
                    color = "green" if side == "long" else "red" if side == "short" else "gray"
                    self.var_pos_label.config(
                        text=f"{side.upper()} {size} @ {entry}",
                        foreground=color
                    )
                else:
                    self.var_pos_label.config(text="포지션 없음", foreground="gray")

                # 담보금
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

                self.log("✓ 조회 완료")

            self.root.after(0, update_ui)

        self._run_async(refresh())

    def run(self):
        """GUI 실행"""
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        """창 닫기"""
        if self.bot and self.bot.running:
            if not messagebox.askyesno("확인", "차익거래가 실행 중입니다. 종료하시겠습니까?"):
                return

        if self.bot:
            self._run_async(self.bot.close())

        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

        self.root.destroy()


def main():
    """메인 함수"""
    import sys
    import os

    # 작업 디렉토리를 스크립트 위치로 설정
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
