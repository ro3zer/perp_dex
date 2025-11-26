from dataclasses import dataclass

@dataclass
class TreadfiHlKey:
    session_cookies: dict
    evm_private_key: str
    main_wallet_address: str
    sub_wallet_address: str
    account_name: str

TREADFIHL_KEY = TreadfiHlKey(
    session_cookies = {"csrftoken":"",
                       "sessionid":""}, # session cookies, can pass
    evm_private_key = '', # your evm address, can pass
    main_wallet_address = '', # required, main address of hyperliquid connected to treadfi
    sub_wallet_address = '', # your subaccount HL address, if not given, given as main_wallet_address
    account_name= '', # your account name of hyperliquid @ traedfi
    )
