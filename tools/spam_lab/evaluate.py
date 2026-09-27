"""탐지기 비교: 오탐(정상 멤버) · 스팸 종류별 잡은 비율 · AI 비용.

    python tools/spam_lab/evaluate.py [--show-fp]     # AI 호출 없음 (llm_judge.py·embed.py 결과 캐시만 읽음)

정상은 두 무리로 나눠 본다 (실제 방 문화: 멤버의 사업 홍보 배너가 허용됨):
  L1 = 실제 멤버의 대화형 단위 + 합성 헷갈리는 정상  → 절대 오탐 0 이어야 함
  L2 = 실제 멤버의 홍보 배너형 단위(관리자가 제재 안 한 광고)  → 방 정책에 따라 오탐일 수도 아닐 수도
문턱은 dev 의 L1 에서 '오탐 0' 을 지키는 가장 낮은 값으로 정하고, test 에 그대로 적용해 보고한다.
--show-fp 는 오탐 단위의 id·종류·이유만 보여준다 (실제 글은 출력 안 함).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from common import UNITS, Cache
from detectors import _EMOJI, CrossRoom, _tokens, rules
from embed import load as load_emb
from llm_judge import lookup

SHOW_FP = "--show-fp" in sys.argv
CATS = ["classic_ad", "llm_paraphrase", "warmup_pitch", "dm_lure", "impersonation", "pig_butchering", "recruitment",
        "multi_room"]


def is_banner(u: dict) -> bool:
    """홍보 배너형 (LLM 없이 모양만): 60자↑ 글에 이모지 3개↑ 또는 @아이디·링크, 또는 5줄↑+이모지 2개↑."""
    for t in u["msgs"]:
        tok = _tokens(t)
        emo = len(_EMOJI.findall(t))
        if len(t) >= 60 and (emo >= 3 or tok["id"] or tok["link"] or tok["invite"]):
            return True
        if t.count("\n") >= 5 and emo >= 2:
            return True
    return False


def load():
    data = json.loads(UNITS.read_text())
    units = data["units"]
    now = data["meta"]["t1"]
    msgs = [dict(m) for m in data["real_msgs"]]
    for u in units:
        if u["source"].startswith("synthetic"):
            for t, ts in zip(u["msgs"], u["ts"]):
                msgs.append({"chat_id": u["chat_id"], "user_id": u["profile"]["user_id"], "text": t, "ts": ts})
    cr = CrossRoom(msgs, window_s=1800, emb=load_emb())
    cache = Cache()
    for u in units:
        u["A"] = rules(u, now)
        u["B"] = cr.check(u)
        b = u["B"]
        u["B_para"] = (b["para_same"] + b["para_other"]) > 0          # 바꿔 쓴 글 (사람은 보통 그대로 복사)
        u["B_other"] = (b["tok_other"] + b["para_other"] + b["copy_other"]) > 0  # 다른 계정과 겹침 (계정 무리)
        u["B_strong"] = u["B_para"] or (b["tok_other"] + b["para_other"]) > 0
        for v in ("v1", "v2", "v3"):
            r = lookup(u, v, cache)
            u[v] = r["p"] if r and r["p"] is not None else None
            u[v + "_cost"] = r["usd"] if r else None
            u[v + "_raw"] = r["data"] if r else None
        raw = u["v3_raw"] or {}
        try:
            u["v3ad"] = min(max(float(raw.get("ad_prob", 0)), 0.0), 1.0)
        except (TypeError, ValueError):
            u["v3ad"] = 0.0
        u["banner"] = is_banner(u)
        u["L"] = ("L2" if u["banner"] else "L1") if u["source"] == "real" else \
            ("L1" if u["source"] == "synthetic_hardneg" else None)
    return data, units


def groups(units):
    g = {"L1_real_convo": [u for u in units if u["source"] == "real" and u["L"] == "L1"],
         "L1_hardneg": [u for u in units if u["source"] == "synthetic_hardneg"],
         "L2_real_banner": [u for u in units if u["source"] == "real" and u["L"] == "L2"],
         "real_newcomer": [u for u in units if u["source"] == "real" and u["newcomer"]],
         "weak_spam_real": [u for u in units if u["source"] == "real_weakspam"]}
    for c in CATS:
        g[c] = [u for u in units if u["source"] == "synthetic" and u["category"] == c]
    return g


def frac(pred, us) -> str:
    return f"{sum(bool(pred(u)) for u in us)}/{len(us)}"


def report(name: str, pred, units, split: str | None = None, show: bool = False) -> dict:
    sel = [u for u in units if split is None or u["split"] == split]
    g = groups(sel)
    row = {"detector": name, "FP L1 real": frac(pred, g["L1_real_convo"]), "FP L1 hardneg": frac(pred, g["L1_hardneg"]),
           "FP real newcomers": frac(pred, g["real_newcomer"]), "L2 banners": frac(pred, g["L2_real_banner"]),
           "weak spam": frac(pred, g["weak_spam_real"])}
    tp = tot = 0
    for c in CATS:
        k = sum(bool(pred(u)) for u in g[c]); tp += k; tot += len(g[c])
        row[c] = f"{k}/{len(g[c])}"
    row["spam all"] = f"{tp}/{tot} ({tp / tot:.0%})" if tot else "-"
    if show:
        for u in g["L1_real_convo"] + g["L1_hardneg"] + g["L2_real_banner"]:
            if pred(u):
                print(f"   FP {u['id']} {u['L']} [{u['category']}] A={u['A']['reasons']} B={u['B']['reasons']} "
                      f"v1={u['v1']} v2={u['v2']} v3={u['v3']} ad={u['v3ad']}")
    return row


def table(rows: list[dict]) -> str:
    cols = list(rows[0])
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def pick(units, score, gate=lambda u: True, legit=("L1",)) -> float:
    """dev 의 정상(L1 기본)에서 gate 를 통과한 것 중 오탐 0 인 가장 낮은 확률 문턱."""
    dev = [u for u in units if u["split"] == "dev" and u["L"] in legit]
    for t in [x / 100 for x in range(5, 101)]:
        if not any(gate(u) and (score(u) or 0) >= t for u in dev):
            return t
    return 1.01


def calib(units, key: str) -> str:
    bins = defaultdict(lambda: [0, 0, 0])
    for u in units:
        if u[key] is None or u["source"] == "real_weakspam":
            continue
        b = min(int(u[key] * 10), 9)
        idx = 2 if u["label"] == "spam" else (1 if u["L"] == "L2" else 0)
        bins[b][idx] += 1
    lines = [f"| {key} 구간 | 정상 L1 | 배너 L2 | 스팸 | 스팸 비율(L1 대비) |", "|---|---|---|---|---|"]
    for b in range(10):
        l1, l2, sp = bins[b]
        if l1 + l2 + sp:
            lines.append(f"| {b / 10:.1f}–{(b + 1) / 10:.1f} | {l1} | {l2} | {sp} | {sp / (l1 + sp):.0%} |"
                         if l1 + sp else f"| {b / 10:.1f}–{(b + 1) / 10:.1f} | {l1} | {l2} | {sp} | - |")
    return "\n".join(lines)


def main() -> None:
    data, units = load()
    g = groups(units)
    print("dataset:", {k: len(v) for k, v in g.items()})
    print("splits:", {s: sum(u["split"] == s for u in units) for s in ("dev", "test")})
    notprof = lambda u: [r for r in u["A"]["reasons"] if r not in ("최근 계정", "아이디 없음", "이모지 이름")]  # noqa: E731

    base = {
        "A any content rule": lambda u: bool(notprof(u)),
        "A hard signal": lambda u: u["A"]["hard"],
        "A score>=4": lambda u: u["A"]["score"] >= 4,
        "A recent account (accountage)": lambda u: u["A"]["recent"],
        "B any cross-room match": lambda u: u["B"]["score"] > 0,
        "B paraphrase (not exact copy)": lambda u: u["B_para"],
        "B other-account overlap": lambda u: u["B_other"],
        "B strong (para OR other-acct token/para)": lambda u: u["B_strong"],
        "C v1 (current scamguard prompt) >=0.8": lambda u: (u["v1"] or 0) >= 0.8,
        "C v2 (newcomer prompt) >=0.8": lambda u: (u["v2"] or 0) >= 0.8,
        "C v3 scam_prob >=0.8": lambda u: (u["v3"] or 0) >= 0.8,
        "C v3 ad_prob >=0.8": lambda u: u["v3ad"] >= 0.8,
    }
    print("\n## 1. 고정 문턱 (전체 = dev+test)")
    print(table([report(n, p, units, show=SHOW_FP) for n, p in base.items()]))

    t1 = pick(units, lambda u: u["v1"]); t2 = pick(units, lambda u: u["v2"]); t3 = pick(units, lambda u: u["v3"])
    t3h = pick(units, lambda u: u["v3"], gate=lambda u: u["A"]["hard"] or u["B_strong"])
    t3all = pick(units, lambda u: u["v3"], legit=("L1", "L2"))
    print(f"\ndev L1 오탐0 문턱: v1 {t1:.2f} · v2 {t2:.2f} · v3 {t3:.2f} · v3(A.hard/B 통과자만) {t3h:.2f} · v3(L1+L2) {t3all:.2f}")
    hard_or_b = lambda u: u["A"]["hard"] or u["B_strong"]  # noqa: E731
    tuned = {
        f"C v1 >={t1:.2f}": lambda u: (u["v1"] or 0) >= t1,
        f"C v2 >={t2:.2f}": lambda u: (u["v2"] or 0) >= t2,
        f"C v3 >={t3:.2f}": lambda u: (u["v3"] or 0) >= t3,
        "C v2>=0.9 AND v3>=0.9 (두 프롬프트 합의)": lambda u: (u["v2"] or 0) >= 0.9 and (u["v3"] or 0) >= 0.9,
        f"ALERT: v3>={t3:.2f} OR ((A.hard OR B strong) AND v3>={t3h:.2f})":
            lambda u: (u["v3"] or 0) >= t3 or (hard_or_b(u) and (u["v3"] or 0) >= t3h),
        "ACT: >=2 of {A.hard, B strong, v3>=0.9}":
            lambda u: (u["A"]["hard"] + u["B_strong"] + ((u["v3"] or 0) >= 0.9)) >= 2,
        "ACT: A.hard AND v2>=0.9 AND v3>=0.9":
            lambda u: u["A"]["hard"] and (u["v2"] or 0) >= 0.9 and (u["v3"] or 0) >= 0.9,
        "ACT: B other-acct AND v3>=0.5":
            lambda u: u["B_other"] and (u["v3"] or 0) >= 0.5,
    }
    for sp in ("dev", "test", None):
        print(f"\n## 2. dev 에서 고른 문턱 → {sp or '전체'}")
        print(table([report(n, p, units, split=sp, show=SHOW_FP and sp is None) for n, p in tuned.items()]))

    print("\n## 3. 보정(calibration), 전체")
    for k in ("v1", "v2", "v3", "v3ad"):
        print(calib(units, k)); print()

    print("## 4. 비용 (신규 입장자 1,000명당, 단위(처음 ≤5개 메시지) 당 1회 호출)")
    real = [u for u in units if u["source"] == "real"]
    for v in ("v1", "v2", "v3"):
        cs = [u[v + "_cost"] for u in units if u[v + "_cost"] is not None]
        rc = [u[v + "_cost"] for u in real if u[v + "_cost"] is not None]
        print(f"{v}: 평균 ${sum(cs) / len(cs):.5f}/호출 → 1,000명 ${1000 * sum(cs) / len(cs):.2f} "
              f"(실제 멤버 글 기준 ${1000 * sum(rc) / len(rc):.2f})")
    gated = [u for u in real if notprof(u) or u["B"]["score"] > 0]
    v3r = sum(u["v3_cost"] for u in real) / len(real)
    print(f"게이트(A 내용 규칙 1개↑ 또는 B 겹침) 통과 — 실제 멤버 {len(gated)}/{len(real)} → v3 게이트 뒤 1,000명 "
          f"${1000 * len(gated) / len(real) * v3r:.2f}")
    syn = [u for u in units if u["source"] == "synthetic"]
    gs = [u for u in syn if notprof(u) or u["B"]["score"] > 0]
    print(f"   (그 게이트는 합성 스팸 {len(gs)}/{len(syn)} 만 통과 → 게이트를 두면 LLM 이 볼 기회 자체가 줄어듦)")
    print(f"\n실제 정상 단위 중 '최근 계정' 추정: {sum(u['A']['recent'] for u in real)}/{len(real)}")
    print(f"실제 정상 중 배너형(L2): {len(g['L2_real_banner'])}/{len(real)}")
    xr = [u for u in real if u["B"]["copy_same"] or u["B"]["tok_same"]]
    print(f"실제 정상 중 30분 안 다른 방에 같은 글/토큰을 같은 계정으로 올린 단위: {len(xr)}/{len(real)}")
    print(f"실제 정상 중 다른 계정과 다른 방에서 겹침(B other): {sum(u['B_other'] for u in real)}/{len(real)}")


if __name__ == "__main__":
    main()
