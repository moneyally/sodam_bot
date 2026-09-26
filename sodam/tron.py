"""트론 주소 도우미 (외부 의존 없음)."""
import hashlib

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # Tether 공식 USDT (TRC20), 소수점 6자리
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def valid_tron_address(addr: str) -> bool:
    """Base58Check + 0x41 접두어 검사. 주소를 한 글자만 틀려도 걸러서 돈을 엉뚱한 곳으로 받지 않게."""
    if not addr or len(addr) != 34 or addr[0] != "T" or any(c not in _B58 for c in addr):
        return False
    n = 0
    for c in addr:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes(25, "big")
    payload, checksum = raw[:21], raw[21:]
    return payload[0] == 0x41 and hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum
