from dataclasses import dataclass

@dataclass
class TreadfiHlKey:
    session_cookies: dict
    evm_private_key: str
    main_wallet_address: str
    sub_wallet_address: str
    account_name: str
    fetch_by_ws: bool

TREADFIHL_KEY = TreadfiHlKey(
    session_cookies = {"csrftoken":"",
                       "sessionid":""}, # session cookies, can skip
    evm_private_key = '', # your evm private key, can skip
    main_wallet_address = '', # required, main address of hyperliquid connected to treadfi
    sub_wallet_address = '', # your subaccount HL address, if not given, given as main_wallet_address
    account_name= '', # your account name of hyperliquid @ traedfi
    fetch_by_ws=True, # use WS_POOL common
    )
