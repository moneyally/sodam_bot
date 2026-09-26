"""텔레그램 연결 인증서 진단 (토큰·키를 쓰지 않음). 실행: python tools/tls_check.py

파이썬 통신만 가로채지는지, 어떤 프록시/프로그램이 끼어드는지 확인한다.
"""
import socket
import ssl
import tempfile
import urllib.request
from pathlib import Path

HOST = "api.telegram.org"

print("OpenSSL:", ssl.OPENSSL_VERSION)
print("시스템 프록시 (파이썬이 따라가는 설정):", urllib.request.getproxies() or "없음")
try:
    ips = sorted({ai[4][0] for ai in socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)})
    print("DNS:", ", ".join(ips))
except OSError as e:
    print("DNS 실패:", e)


def peer_cert(sock_factory, label):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 진단용: 받은 인증서를 보기만 하고 아무 데이터도 보내지 않음
    try:
        with ctx.wrap_socket(sock_factory(), server_hostname=HOST) as s:
            der = s.getpeercert(binary_form=True)
            pem = Path(tempfile.gettempdir()) / "tg_peer.pem"
            pem.write_text(ssl.DER_cert_to_PEM_cert(der))
            info = ssl._ssl._test_decode_cert(str(pem))
            issuer = ", ".join(f"{k}={v}" for rdn in info["issuer"] for k, v in rdn)
            print(f"[{label}] 발급자: {issuer}")
            print(f"[{label}] 접속 IP: {s.getpeername()[0]} / {s.version()} / 인증서 크기 {len(der)} bytes")
    except OSError as e:
        print(f"[{label}] 연결 실패: {e}")


# 1) 프록시를 거치지 않고 직접 연결
peer_cert(lambda: socket.create_connection((HOST, 443), timeout=10), "직접 연결")

# 2) 실제 검증 (봇이 하는 것과 같은 방식)
try:
    with socket.create_connection((HOST, 443), timeout=10) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=HOST):
            print("[정상 검증] 통과 ✅ — 직접 연결은 안전함")
except ssl.SSLError as e:
    print("[정상 검증] 실패 ❌:", e.reason if hasattr(e, "reason") else e)
