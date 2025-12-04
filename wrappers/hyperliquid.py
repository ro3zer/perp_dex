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

    async def get_position(self, symbol):
        pass
    
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

        # ---------------- Perp: clearinghouseState (DEX 집계) ----------------
        dex_order = list(dict.fromkeys(self.dex_list or ["hl"]))  # 중복 제거 + 순서 유지
        # 'hl' 은 API에서 빈 문자열("")이 첫 perp dex을 의미
        def _dex_param(name: str) -> str:
            k = (name or "").strip().lower()
            return "" if (k == "" or k == "hl") else k

        async def _fetch_ch(dex_name: str) -> tuple[float, float]:
            payload = {"type": "clearinghouseState", "user": address}
            dp = _dex_param(dex_name)
            if dp != "":
                payload["dex"] = dp
            else:
                payload["dex"] = ""  # 명시적으로 첫 DEX
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

        # 병렬 요청
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

            if balances:
                spot_usdc = float(balances.get("USDC",0))
                spot_usdh = float(balances.get("USDH",0))
                spot_usdt = float(balances.get("USDT0",0))
            else:
                spot_usdc = None
                spot_usdh = None
                spot_usdt = None
                
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
        
        if balances:
            spot_usdc = float(balances.get("USDC",0))
            spot_usdh = float(balances.get("USDH",0))
            spot_usdt = float(balances.get("USDT0",0))
        else:
            spot_usdc = None
            spot_usdh = None
            spot_usdt = None

        return {
            "available_collateral": available_collateral,
            "total_collateral": total_collateral,
            "spot": {
                "USDH": spot_usdh,
                "USDC": spot_usdc,
                "USDT": spot_usdt,
            },
        }

    async def get_open_orders(self, symbol):
        pass
    
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

    async def get_open_orders(self, symbol):
        pass

async def test():
    hl = HyperliquidExchange()
    await hl.init()
    print(hl.spot_index_to_name)
    print(hl.spot_name_to_index)

if __name__ == "__main__":
    asyncio.run(test())