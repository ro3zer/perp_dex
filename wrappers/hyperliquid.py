from multi_perp_dex import MultiPerpDex, MultiPerpDexMixin
from .hyperliquid_ws_client import HLWSClientRaw, WS_POOL
import json
from typing import Dict, Optional, List, Dict, Tuple
import aiohttp
from aiohttp import TCPConnector
import asyncio
import time

BASE_URL = "https://api.hyperliquid.xyz"
BASE_WS = "wss://api.hyperliquid.xyz/ws"
STABLES = ["USDC","USDT0","USDH"]

class HyperliquidExchange(MultiPerpDexMixin, MultiPerpDex):
    def __init__(self, 
              wallet_address = None,        # required
              wallet_private_key = None,    # optional, required when by_agent = False
              agent_api_address = None,     # optional, required when by_agent = True
              agent_api_private_key = None, # optional, required when by_agent = True
              by_agent = True,              # recommend to use True
              vault_address = None,         # optional, sub-account address
              builder_code = None,
              builder_fee_pair: dict = None,     # {"base","dex"# optional,"xyz" # optional,"vntl" #optional,"flx" #optional}
              *,
              fetch_by_ws = False, # fetch pos, balance, and price by ws client
              # ws_client = None, # ws client가 외부에서 생성됐으면 그걸 사용, acquire 알고리즘으로 불필요
              # ws_client의 경우 WS_POOL 하나를 공유
              # signing_method = None, # special case: superstack, tread.fi, 분리?
              ):

        self.by_agent = by_agent
        self.wallet_address = wallet_address

        # need error check
        if self.by_agent == False:
            self.wallet_private_key = wallet_private_key
            self.agent_api_address = None
            self.agent_api_private_key = None
        else:
            self.wallet_private_key = None
            self.agent_api_address = agent_api_address
            self.agent_api_private_key = agent_api_private_key

        self.vault_address = vault_address
        self.builder_code = self._get_builder_code(builder_code)
        self.builder_fee_pair = builder_fee_pair
        
        self.http_base = BASE_URL
        self.ws_base = BASE_WS
        self.spot_index_to_name = None
        self.spot_name_to_index = None
        self.spot_asset_index_to_pair = None
        self.spot_asset_pair_to_index = None
        self.spot_asset_index_to_bq = None
        self.spot_prices = None
        self.dex_list = ['hl', 'xyz', 'flx', 'vntl', 'hyna'] # default

        self._http =  None

        # WS 관련 내부 상태
        self.ws_client: Optional[HLWSClientRaw] = None  # WS_POOL에서
        self._ws_pool_key = None                        # comment: release 시 사용
        
        self._ws_init_lock = asyncio.Lock()             # comment: create_ws_client 중복 호출 방지
        self.fetch_by_ws = fetch_by_ws
        #self.signing_method = signing_method

    def _get_builder_code(self, builder_code:str = None):
        if builder_code:
            if builder_code.startswith('0x'):
                return builder_code # fallback to original
            else:
                match builder_code.lower():
                    case 'lit' | 'lit.trade' | 'littrade':
                        return "0x24a747628494231347f4f6aead2ec14f50bcc8b7"
                    case 'based' | 'basedone' | 'basedapp':
                        return "0x1924b8561eef20e70ede628a296175d358be80e5"
                    case 'dexari':
                        return "0x7975cafdff839ed5047244ed3a0dd82a89866081"
                    case 'liquid':
                        return "0x6d4e7f472e6a491b98cbeed327417e310ae8ce48"
                    case 'supercexy':
                        return "0x0000000bfbf4c62c43c2e71ef0093f382bf7a7b4"
                    case 'bullpen':
                        return "0x4c8731897503f86a2643959cbaa1e075e84babb7"
                    case 'mass':
                        return "0xf944069b489f1ebff4c3c6a6014d58cbef7c7009"
                    case 'dreamcash':
                        return "0x4950994884602d1b6c6d96e4fe30f58205c39395"
                    #case 'superstack':
                    #    return "0xcdb943570bcb48a6f1d3228d0175598fea19e87b"
                    #case 'tread.fi' | 'treadfi':
                    #    return "0x999a4b5f268a8fbf33736feff360d462ad248dbf"
    
    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                connector=TCPConnector(
                    force_close=True,             # 매 요청 후 소켓 닫기 → 종료 시 잔여 소켓 최소화
                    enable_cleanup_closed=True,   # 종료 중인 SSL 소켓 정리 보조 (로그 억제)
                )
            )
        return self._http
    
    async def close(self):
        # HTTP 세션 종료 + WS 풀 release
        if self._http and not self._http.closed:
            await self._http.close()
        # WS 풀 release: 이 인스턴스에서 acquire한 경우에만 해제
        if self._ws_pool_key:
            ws_url, addr = self._ws_pool_key
            try:
                await WS_POOL.release(ws_url=ws_url, address=addr)  # comment: 참조 카운트 -1
            except Exception:
                pass
            finally:
                self._ws_pool_key = None
                self.ws_client = None

    async def init(self):
        await self._init_spot_token_map() # spot meta
        await self._get_dex_list()        # perpDexs 리스트 (webData3 순서)
        
        try:
            await WS_POOL.prime_shared_meta(
                dex_order=self.dex_list or ["hl"],
                idx2name=self.spot_index_to_name or {},
                name2idx=self.spot_name_to_index or {},
                pair_by_index=self.spot_asset_index_to_pair or {},
                bq_by_index=self.spot_asset_index_to_bq or {},
            )
        except Exception:
            pass
        
        # reverse id
        self.spot_asset_pair_to_index = {
            v: k for k, v in (self.spot_asset_index_to_pair or {}).items()
        }
        
        if self.fetch_by_ws:
            await self.create_ws_client()

        return self

    async def _get_dex_list(self):
        url = f"{self.http_base}/info"
        payload = {"type":"perpDexs"}
        headers = {"Content-Type": "application/json"}
        s = self._session()
        async with s.post(url, json=payload, headers=headers) as r:
            try:
                resp = await r.json()
            except aiohttp.ContentTypeError:
                return
        # [CHANGED] 순서 유지 + 중복 제거 + lower 정규화
        order = ["hl"]  # HL 항상 선두
        seen = set(["hl"])
        if isinstance(resp, list):
            for e in resp:
                if not isinstance(e, dict):
                    continue
                n = e.get("name")
                if not n:
                    continue
                k = str(n).lower().strip()
                if k and k not in seen:
                    order.append(k); seen.add(k)
        self.dex_list = order

    async def _init_spot_token_map(self):
        """
        REST info(spotMeta)를 통해
        - 토큰 인덱스 <-> 이름(USDC, PURR, ...) 맵
        - 스팟 페어 인덱스(spotInfo.index) <-> 'BASE/QUOTE' 및 (BASE, QUOTE) 맵
        을 1회 로드/갱신한다.
        """

        url = f"{self.http_base}/info"
        payload = {"type": "spotMeta"}
        headers = {"Content-Type": "application/json"}

        s = self._session()
        async with s.post(url, json=payload, headers=headers) as r:
            status = r.status
            try:
                resp = await r.json()
            except aiohttp.ContentTypeError:
                # 실패 시 빈 맵으로 초기화하고 반환
                self.spot_index_to_name = {}
                self.spot_name_to_index = {}
                self.spot_asset_index_to_pair = {}
                self.spot_asset_index_to_bq = {}
                return
        
        # 안전 가드: dict 응답인지 확인
        if not isinstance(resp, dict):
            self.spot_index_to_name = {}
            self.spot_name_to_index = {}
            self.spot_asset_index_to_pair = {}
            self.spot_asset_index_to_bq = {}
            return
        
        tokens = (resp or {}).get("tokens") or []
        universe = (resp or {}).get("universe") or (resp or {}).get("spotInfos") or []

        # 1) 토큰 맵(spotMeta.tokens[].index -> name)
        idx2name: Dict[int, str] = {}
        name2idx: Dict[str, int] = {}
        for t in tokens:
            if isinstance(t, dict) and "index" in t and "name" in t:
                try:
                    idx = int(t["index"])
                    name = str(t["name"]).upper().strip()
                    if not name:
                        continue
                    idx2name[idx] = name
                    name2idx[name] = idx
                except Exception as ex:
                    pass
            #print(name,idx)
        self.spot_index_to_name = idx2name
        self.spot_name_to_index = name2idx
        
        
        # 2) 페어 맵(spotInfo.index -> 'BASE/QUOTE' 및 (BASE, QUOTE))
        pair_by_index: Dict[int, str] = {}
        bq_by_index: Dict[int, tuple[str, str]] = {}
        ok = 0
        fail = 0
        for si in universe:
            if not isinstance(si, dict):
                continue
            # 필수: spotInfo.index
            try:
                s_idx = int(si.get("index"))
            except Exception:
                fail += 1
                continue

            # 우선 'tokens': [baseIdx, quoteIdx] 배열 처리
            base_idx = None
            quote_idx = None
            toks = si.get("tokens")
            if isinstance(toks, (list, tuple)) and len(toks) >= 2:
                try:
                    base_idx = int(toks[0])
                    quote_idx = int(toks[1])
                except Exception:
                    base_idx, quote_idx = None, None

            # 보조: 환경별 키(base/baseToken/baseTokenIndex, quote/...)
            if base_idx is None:
                bi = si.get("base") or si.get("baseToken") or si.get("baseTokenIndex")
                try:
                    base_idx = int(bi) if bi is not None else None
                except Exception:
                    base_idx = None
            if quote_idx is None:
                qi = si.get("quote") or si.get("quoteToken") or si.get("quoteTokenIndex")
                try:
                    quote_idx = int(qi) if qi is not None else None
                except Exception:
                    quote_idx = None

            base_name = idx2name.get(base_idx) if base_idx is not None else None
            quote_name = idx2name.get(quote_idx) if quote_idx is not None else None

            # name 필드가 'BASE/QUOTE'면 그대로, '@N' 등인 경우 토큰명으로 합성
            name_field = si.get("name")
            pair_name = None
            if isinstance(name_field, str) and "/" in name_field:
                pair_name = name_field.strip().upper()
                # base/quote 이름 보완
                try:
                    b, q = pair_name.split("/", 1)
                    base_name = base_name or b
                    quote_name = quote_name or q
                except Exception:
                    pass
            else:
                if base_name and quote_name:
                    pair_name = f"{base_name}/{quote_name}"

            if pair_name and base_name and quote_name:
                pair_by_index[s_idx] = pair_name
                bq_by_index[s_idx] = (base_name, quote_name)
                ok += 1
            else:
                fail += 1
            #print(base_name,quote_name)
        
        self.spot_asset_index_to_pair = pair_by_index
        self.spot_asset_index_to_bq = bq_by_index

    async def create_ws_client(self):
        """
        WS 커넥션을 '1회 연결 + 다중 구독'으로 운용.
        - 전역 풀(WS_POOL)에서 (ws_url,address) 키로 하나를 획득하여 공유
        - 인스턴스 내부에서 중복 acquire를 방지
        """
        async with self._ws_init_lock:
            if self.ws_client is not None:
                return self.ws_client
            
            address = self.vault_address if self.vault_address else self.wallet_address
            # acquire에 메타를 전달(풀 내부에서 최초 1회만 반영)
            client = await WS_POOL.acquire(
                ws_url=self.ws_base,
                http_base=self.http_base,
                address=address,
                dex=None,
                dex_order=self.dex_list or ["hl"],
                idx2name=self.spot_index_to_name or {},
                name2idx=self.spot_name_to_index or {},
                pair_by_index=self.spot_asset_index_to_pair or {},
                bq_by_index=self.spot_asset_index_to_bq or {},
            )
            # 추가 DEX 구독
            for dex in (self.dex_list or []):
                if dex != "hl":
                    await client.ensure_allmids_for(dex)

            self.ws_client = client
            self._ws_pool_key = (self.ws_base, (address or "").lower())
            return self.ws_client

    async def create_order(self, symbol, side, amount, price=None, order_type='market'):
        pass

    # 포지션 파싱 공통 헬퍼
    def _parse_position_core(self, pos: dict) -> dict:
        """
        clearinghouseState.assetPositions[*].position 또는 WS 정규화 포맷을
        표준 스키마로 변환합니다.
        반환 스키마:
        {"entry_price": float|None, "unrealized_pnl": float|None, "side": "long"|"short"|"flat", "size": float}
        """
        def fnum(x, default=None):
            try:
                return float(x)
            except Exception:
                return default

        # WS 정규화 포맷(이미 float) 대응
        if "entry_px" in pos or "upnl" in pos or "size" in pos:
            size = fnum(pos.get("size"), 0.0) or 0.0
            side = pos.get("side") or ("long" if size > 0 else ("short" if size < 0 else "flat"))
            return {
                "entry_price": fnum(pos.get("entry_px")),
                "unrealized_pnl": fnum(pos.get("upnl"), 0.0),
                "side": side,
                "size": abs(size),
            }

        # REST 원본 포맷 대응
        size_signed = fnum(pos.get("szi"), 0.0) or 0.0
        side = "long" if size_signed > 0 else ("short" if size_signed < 0 else "flat")
        return {
            "entry_price": fnum(pos.get("entryPx")),
            "unrealized_pnl": fnum(pos.get("unrealizedPnl"), 0.0),
            "side": side,
            "size": abs(size_signed),
        }
    
    async def get_position(self, symbol):
        """
        주어진 perp 심볼에 대한 단일 포지션 요약을 반환합니다.
        반환 스키마:
          {"entry_price": float|None, "unrealized_pnl": float|None, "side": "long"|"short"|"flat", "size": float}
        """
        if self.fetch_by_ws:
            try:
                pos = await self.get_position_ws(symbol, timeout=2.0)
                if pos is not None:
                    return pos
            except Exception:
                pass
        return await self.get_position_rest(symbol)
    
    async def get_position_ws(self, symbol: str, timeout: float = 2.0, dex: str | None = None) -> dict:
        """
        webData3(WS 캐시)에서 조회. 스냅샷 미도착 시 timeout까지 짧게 대기합니다.
        dex를 지정하지 않으면 self.dex_list 순서대로 검색합니다.
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return None

        if not self.ws_client:
            await self.create_ws_client()

        # 스냅샷 대기(간단 폴링)
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if getattr(self.ws_client, "positions_by_dex_norm", None):
                break
            await asyncio.sleep(0.05)

        sym = str(symbol).strip().upper()
        # 현재 캐시에 있는 키 기반으로 순회
        if dex:
            dex_keys = [str(dex).lower()]
        else:
            dex_keys = list(getattr(self.ws_client, "positions_by_dex_norm", {}).keys())

        for dk in dex_keys:
            pos_map = (self.ws_client.positions_by_dex_norm or {}).get(dk) or {}
            pos = pos_map.get(sym)
            if not pos:
                continue
            parsed = self._parse_position_core(pos)
            if parsed["size"] and parsed["side"] != "flat":
                return parsed
        return None
    
    async def get_position_rest(self, symbol: str, dex: str | None = None) -> dict:
        """
        REST clearinghouseState를 dex별로 조회하여 포지션을 찾습니다.
        dex를 지정하지 않으면 self.dex_list 순서대로 검색합니다.
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return None

        url = f"{self.http_base}/info"
        headers = {"Content-Type": "application/json"}
        s = self._session()

        def _dex_param(name: Optional[str]) -> str:
            k = (name or "").strip().lower()
            return "" if (k == "" or k == "hl") else k

        sym = str(symbol).strip().upper()
        dex_iter = [dex] if dex else list(dict.fromkeys(self.dex_list or ["hl"]))

        for d in dex_iter:
            payload = {"type": "clearinghouseState", "user": address, "dex": _dex_param(d)}
            try:
                async with s.post(url, json=payload, headers=headers) as r:
                    data = await r.json()
            except aiohttp.ContentTypeError:
                continue
            except Exception:
                continue

            aps = (data or {}).get("assetPositions") or []
            for ap in aps:
                pos = (ap or {}).get("position") or {}
                coin = str(pos.get("coin") or "").upper()
                if coin != sym:
                    continue
                parsed = self._parse_position_core(pos)
                if parsed["size"] and parsed["side"] != "flat":
                    return parsed

        return None
    
    async def close_position(self, symbol, position):
        pass
    
    async def get_collateral(self):
        if self.fetch_by_ws:
            try:
                return await self.get_collateral_ws()
            except:
                pass
        
        # fall back to rest api
        try:
            return await self.get_collateral_rest()
        except:
            return {
                "available_collateral":None,
                "total_collateral": None,
                "spot":{
                    "USDH":None,
                    "USDC":None,
                    "USDT":None
                }
            }
    
    async def get_collateral_rest(self):
        """
        REST 기반 담보 조회(WS 폴백용):
        - Perp: POST {http_base}/info {"type":"clearinghouseState", "user": <addr>, "dex": <""|name>}
                 → marginSummary.accountValue, withdrawable 합산
        - Spot: POST {http_base}/info {"type":"spotClearinghouseState", "user": <addr>}
                 → balances[].total 중 스테이블만 추출(USDC, USDT/USDT0, USDH)

        반환: {
          "available_collateral": float|None,
          "total_collateral": float|None,
          "spot": {"USDH": float|None, "USDC": float|None, "USDT": float|None}
        }
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return {
                "available_collateral": None,
                "total_collateral": None,
                "spot": {"USDH": None, "USDC": None, "USDT": None},
            }

        url = f"{self.http_base}/info"
        headers = {"Content-Type": "application/json"}
        s = self._session()

        # ---------------- Perp: clearinghouseState 집계 ----------------
        def _dex_param(name: Optional[str]) -> str:
            k = (name or "").strip().lower()
            return "" if (k == "" or k == "hl") else k

        dex_order = list(dict.fromkeys(self.dex_list or ["hl"]))  # 순서 유지 + 중복 제거

        async def _fetch_ch(dex_name: str) -> tuple[float, float]:
            payload = {"type": "clearinghouseState", "user": address, "dex": _dex_param(dex_name)}
            try:
                async with s.post(url, json=payload, headers=headers) as r:
                    data = await r.json()
            except aiohttp.ContentTypeError:
                return (0.0, 0.0)
            except Exception:
                return (0.0, 0.0)
            try:
                ms = (data or {}).get("marginSummary") or {}
                av = float(ms.get("accountValue") or 0.0)
            except Exception:
                av = 0.0
            try:
                wd = float((data or {}).get("withdrawable") or 0.0)
            except Exception:
                wd = 0.0
            return (av, wd)

        # 병렬 호출
        perp_results = await asyncio.gather(*[_fetch_ch(d) for d in dex_order], return_exceptions=False)
        av_sum = sum(av for av, _ in perp_results)
        wd_sum = sum(wd for _, wd in perp_results)

        total_collateral = av_sum if av_sum != 0.0 else None
        available_collateral = wd_sum if wd_sum != 0.0 else None

        # ---------------- Spot: spotClearinghouseState ----------------
        spot_usdc = spot_usdh = spot_usdt = None
        try:
            payload_spot = {"type": "spotClearinghouseState", "user": address}
            async with s.post(url, json=payload_spot, headers=headers) as r:
                spot_resp = await r.json()
            balances_list = (spot_resp or {}).get("balances") or []
            balances = {}
            for b in balances_list:
                if not isinstance(b, dict):
                    continue
                name = str(b.get("coin") or b.get("tokenName") or b.get("token") or "").upper()
                try:
                    total = float(b.get("total") or 0.0)
                except Exception:
                    continue
                if name:
                    balances[name] = total

            spot_usdc = float(balances.get("USDC", 0.0))   # USDC 없으면 0.0
            spot_usdh = float(balances.get("USDH", 0.0))   # USDH 없으면 0.0
            spot_usdt = float(balances.get("USDT0", 0.0))  # 항상 USDT0 사용
        except aiohttp.ContentTypeError:
            pass
        except Exception:
            pass

        return {
            "available_collateral": available_collateral,
            "total_collateral": total_collateral,
            "spot": {
                "USDH": spot_usdh,
                "USDC": spot_usdc,
                "USDT": spot_usdt,
            },
        }
    
    async def get_collateral_ws(self, timeout: float = 2.0):
        """
        WS(webData3/spotState) 기반 담보 조회.
        - 주소가 설정되어 있어야 하며, 첫 스냅샷이 도착할 때까지 최대 timeout 초 대기.
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return {
                "available_collateral": None,
                "total_collateral": None,
                "spot": {"USDH": None, "USDC": None, "USDT": None},
            }

        if not self.ws_client:
            await self.create_ws_client()

        # 1) webData3/spotState 첫 스냅샷을 짧게 폴링 대기
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            has_margin = bool(getattr(self.ws_client, "margin_by_dex", {}))
            has_bal = bool(getattr(self.ws_client, "balances", {}))
            if has_margin and has_bal:
                break
            await asyncio.sleep(0.05)

        # 2) DEX별 합산
        av_sum = 0.0
        wd_sum = 0.0
        try:
            for d, m in (self.ws_client.margin_by_dex or {}).items():
                try:
                    av_sum += float((m or {}).get("accountValue") or 0.0)
                except Exception:
                    pass
                try:
                    wd_sum += float((m or {}).get("withdrawable") or 0.0)
                except Exception:
                    pass
        except Exception:
            pass

        total_collateral = av_sum if av_sum != 0.0 else None
        available_collateral = wd_sum if wd_sum != 0.0 else None

        # 3) 스팟 스테이블 잔고
        balances = {}
        try:
            balances = self.ws_client.get_all_spot_balances()
        except Exception:
            balances = dict(getattr(self.ws_client, "balances", {}))
        
        spot_usdc = float(balances.get("USDC", 0.0))
        spot_usdh = float(balances.get("USDH", 0.0))
        spot_usdt = float(balances.get("USDT0", 0.0))

        return {
            "available_collateral": available_collateral,
            "total_collateral": total_collateral,
            "spot": {
                "USDH": spot_usdh,
                "USDC": spot_usdc,
                "USDT": spot_usdt,
            },
        }

    def _normalize_open_order_rest(self, o: dict) -> Optional[dict]:
        if not isinstance(o, dict):
            return None
        coin_raw = str(o.get("coin") or "")
        # 스팟 페어 인덱스 → 'BASE/QUOTE'
        if coin_raw.startswith("@"):
            try:
                pair_idx = int(coin_raw[1:])
            except Exception:
                return None
            pair = (self.spot_asset_index_to_pair or {}).get(pair_idx)
            if not pair:
                return None
            symbol = str(pair).upper()
        else:
            symbol = coin_raw.upper()

        def fnum(x, default=None):
            try:
                return float(x)
            except Exception:
                return default

        out = {
            "order_id": o.get("oid"),
            "symbol": symbol,
            "side": "short" if o.get("side") == 'A' else 'long',
            "price": fnum(o.get("limitPx")),
            "size": fnum(o.get("sz")),
            #"timestamp": int(o.get("timestamp")) if o.get("timestamp") is not None else None,
            #"raw": o,
        }
        return out if out["order_id"] is not None and out["symbol"] else None

    async def get_open_orders_ws(self, symbol: str, timeout: float = 2.0) -> Optional[List[dict]]:
        """
        WS openOrders 캐시에서 주어진 심볼의 미체결 주문을 반환.
        - 구독이 없으면 subscribe를 보장하고, 초기 스냅샷을 timeout까지 대기(폴링).
        - 없으면 None.
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return None

        if not self.ws_client:
            await self.create_ws_client()

        if hasattr(self.ws_client, "wait_open_orders_ready"):
            ok = await self.ws_client.wait_open_orders_ready(timeout=timeout)
            if not ok:
                # 이벤트가 없는 구현/타임아웃이면 폴링으로 후퇴
                pass

        # 폴링 대기(백업 경로)
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            lst = getattr(self.ws_client, "open_orders", None)
            if isinstance(lst, list):
                break
            await asyncio.sleep(0.05)

        orders = list(getattr(self.ws_client, "open_orders", []) or [])
        
        if not orders:
            return None
        
        sym = str(symbol).upper().strip()
        filtered = [o for o in orders if (o.get("symbol") or "").upper() == sym]
        return filtered or None
    
    async def get_open_orders_rest(self, symbol: str, dex: str = "ALL_DEXS") -> Optional[List[dict]]:
        """
        REST openOrders 조회 후 주어진 심볼로 필터링하여 반환.
        - 없으면 None.
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return None

        url = f"{self.http_base}/info"
        headers = {"Content-Type": "application/json"}
        payload = {"type": "openOrders", "user": address, "dex": dex}

        s = self._session()
        try:
            async with s.post(url, json=payload, headers=headers) as r:
                resp = await r.json()
        except aiohttp.ContentTypeError:
            return None
        except Exception:
            return None

        # 응답 포맷: {"orders":[...]} 또는 바로 리스트([...]) 케이스 방어
        orders_raw = []
        if isinstance(resp, dict) and isinstance(resp.get("orders"), list):
            orders_raw = resp.get("orders")
        elif isinstance(resp, list):
            orders_raw = resp
        else:
            return None

        normalized = []
        for o in orders_raw:
            no = self._normalize_open_order_rest(o)
            if no:
                normalized.append(no)
        if not normalized:
            return None

        sym = str(symbol).upper().strip()
        filtered = [o for o in normalized if (o.get("symbol") or "").upper() == sym]
        return filtered or None

    async def get_open_orders(self, symbol: str) -> Optional[List[dict]]:
        if self.fetch_by_ws:
            try:
                res = await self.get_open_orders_ws(symbol, timeout=2.0)
                if res:
                    return res
            except Exception:
                pass
        return await self.get_open_orders_rest(symbol, dex="ALL_DEXS")

    async def cancel_orders(self, symbol):
        pass

    # 내부 헬퍼: Spot 후보 페어 생성(우선순위 고정)
    def _spot_pair_candidates(self, raw_symbol: str) -> list[str]:
        """
        'BASE/QUOTE'면 그대로 1개, 아니면 STABLES 우선순위로 BASE/QUOTE 후보를 만든다.
        """
        rs = str(raw_symbol).strip()
        if "/" in rs:
            return [rs.upper()]
        base = rs.upper()
        return [f"{base}/{q}" for q in STABLES]

    async def get_mark_price(self,symbol,*,is_spot=False):
        raw = str(symbol).strip()
        if "/" in raw:
            is_spot = True # auto redirect

        if self.fetch_by_ws:
            try:
                px = await self.get_mark_price_ws(symbol, is_spot=is_spot, timeout=2)
                return float(px)
            except Exception as e:
                pass
        
        # default rest api
        try:
            px = await self.get_mark_price_rest(symbol, is_spot=is_spot)
            return float(px) if px is not None else None
        except Exception as e:
            return None
    
    async def get_mark_price_rest(self,symbol,*,is_spot=False):
        dex = None
        if ":" in symbol:
            dex = symbol.split(":")[0].lower()
        
        url = f"{self.http_base}/info"
        headers = {"Content-Type": "application/json"}

        if is_spot:
            payload = {"type":"spotMetaAndAssetCtxs"}
        else:
            payload = {"type":"metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
        
        
        s = self._session()
        async with s.post(url, json=payload, headers=headers) as r:
            status = r.status
            try:
                resp = await r.json()
            except aiohttp.ContentTypeError:
                # 비-JSON이면 폴백 불가 → None
                return None

        universe = resp[0].get("universe") if isinstance(resp, list) and len(resp) >= 2 and isinstance(resp[0], dict) else None
        meta = resp[1] if isinstance(resp, list) and len(resp) >= 2 else None
        
        if universe is None or meta is None:
            return None

        if is_spot:
            for pair in self._spot_pair_candidates(symbol.upper()):
                spot_idx = self.spot_asset_pair_to_index.get(pair)
                if spot_idx is None:
                    # UBTC, UETH, ..., 외부에서 pair 검증해도 이 부분은 유지
                    spot_idx = self.spot_asset_pair_to_index.get(f"U{pair}")
                try:
                    price = meta[spot_idx].get('markPx')
                    #print(price, pair)
                    return price # USDC, USDT, USDH 순으로 찾아서 먼저 나오는거
                except:
                    continue

            return None
                
        else:
            for idx, value in enumerate(universe):
                if value.get('name').upper() == symbol.upper():
                    #print(idx, value.get('name'), symbol)
                    price = meta[idx].get('markPx')
                    return price
        
        return None
    
    async def get_mark_price_ws(self,symbol, *, is_spot=False, timeout: float = 3.0):
        """
        WS 캐시 기반 마크 프라이스 조회.
        - is_spot=True 이면 'BASE/QUOTE' 페어 가격을 조회
        - is_spot=False 이면 perp(예: 'BTC') 가격을 조회
        - 첫 틱이 아직 도착하지 않은 경우 wait_price_ready가 있으면 timeout까지 대기
        - 값을 얻지 못하면 예외를 던져 상위(get_mark_price)에서 REST 폴백하게 한다.
        """
        if not self.ws_client:
            await self.create_ws_client()

        raw = str(symbol).strip()
        #if "/" in raw:
        #    is_spot = True

        if is_spot:
            for pair in self._spot_pair_candidates(raw.upper()):
                # spot_pair로 명시
                if hasattr(self.ws_client, "wait_price_ready"):
                    try:
                        ready = await asyncio.wait_for(
                            self.ws_client.wait_price_ready(pair, timeout=timeout, kind="spot_pair"),
                            timeout=timeout
                        )
                        if not ready:
                            continue
                    except Exception:
                        continue
                
                px = self.ws_client.get_spot_pair_px(pair)
                if px is not None:
                    return float(px)

            # 모든 후보 실패
            raise TimeoutError(f"WS spot price not ready. tried={self._spot_pair_candidates(raw.upper())}")

        # Perp 경로
        key = raw.upper()
        # perp로 명시
        try:
            await asyncio.wait_for(
                self.ws_client.wait_price_ready(key, timeout=timeout, kind="perp"),
                timeout=timeout
            )
        except Exception:
            pass

        px = self.ws_client.get_price(key)
        if px is None:
            raise TimeoutError(f"WS perp price not ready for {key}")
        return float(px)

async def test():
    hl = HyperliquidExchange()
    await hl.init()
    print(hl.spot_index_to_name)
    print(hl.spot_name_to_index)

if __name__ == "__main__":
    asyncio.run(test())