"""🧠 소담이 왜 틀렸나 — AI 실행 기록(agent_runs)에서 '어디서 꼬였는지'를 코드로 찾는다 (AI 호출 0원).

오너 결정 2026-10-03: 개발자(오너·Claude)만 보는 실수 분석. 멤버·방 관리자에겐 안 보임.
- 기록: agentlog.Run 이 실행마다 단계(events: 길·올려 보냄·검사·상한)와 도구마다 관문 결과(gate)·쓰기 여부(w)·최종 답(answer)을 남김.
- 판정(analyze): 답은 '했어요'인데 실제로 된 쓰기가 없음 · 도구 오류 · 찾기 실패로 끝남 · 기능 꺼짐 · 시간/요금 상한 ·
  같은 사람이 10분 안에 비슷하게 다시 요청 · 답 뒤 3분 안 불만('안 되네' 등, 소담에게 답장했거나 이름을 부른 것만).
- 보는 곳: 원격 점검 창구 `why` (Claude, diag.py — 이 파일은 표준 라이브러리만 씀), 오너 1:1 하루 한 번 (mistakes.py).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# 도구를 하나도 안 불렀는데/실제 쓰기 없이 '했어요' (agent 의 보내기 전 검사도 이걸 씀)
CLAIM = re.compile(r"(뮤트|밴|경고|차단|내보냈|예약|등록|저장|삭제|지웠|켰|껐|바꿨|보냈|걸어|걸었|알림|태그|추가|해제|올렸)[^\n.?!]{0,6}"
                   r"(했어|했습니다|완료|처리했|해\s?드렸|뒀어|놨어|뒀습니다|됐어요|되었습니다)"
                   # 멈춤 주장 (2026-10-03 일루왕: 전체 태그 중 멤버 '소담아 멈춰' → 도구 없이 '멈췄습니다' 두 번, 실제론 256명 끝까지)
                   r"|멈췄|멈춘\s?거|중지했|중단했|그만뒀")

# 도구 결과 → 관문 (tools.execute·각 도구가 돌려주는 정해진 문구 기준)
_GATES = (
    ("security", ("(보안", "보안 —", "숨은 지시")),
    ("perm", ("권한 없음", "관리자만", "오너만", "권한이 없", "'사용자 차단' 권한")),
    ("off", ("꺼져 있는 기능", "꺼져 있음", "이용 기간")),
    ("limit", ("한도", "다 썼", "다 써서")),
    ("card", ("확인 카드", "확인 버튼", "버튼을 방에", "카드를 방에", "카드 올림", "할까요")),
    ("error", ("실행 중 오류", "도구 입력 오류", "입력 형식 오류", "오류가 났")),
    ("soft", ("찾을 수 없", "못 찾", "찾지 못", "특정하지 못", "기록 없음", "기록이 없음", "결과 없음", "여러 명이",
              "여러 개 찾음", "사용할 수 없음", "지금은 못", "못 가져옴", "고칠 사진이 없음")),
)
GATE_KO = {"ok": "통과", "security": "보안 규칙에 막힘", "perm": "권한 없어서 막힘", "off": "기능 꺼짐/이용 기간",
           "limit": "한도", "card": "확인 카드 올림", "error": "도구 오류", "soft": "못 찾음/실패"}
LANE_KO = {"light": "작은 모델", "heavy": "큰 모델", "banter": "큰 모델(말싸움)"}
STAGE = {"claim": "답변", "error": "실행", "soft": "찾기", "off": "설정", "cap": "시간·요금 상한", "redo": "결과",
         "complaint": "결과", "empty": "답변", "crash": "실행"}
REDO_SEC = 600
COMPLAINT_SEC = 180
# 한 번 잘 된 뒤 또 부탁하는 게 자연스러운 일 (게임 한 판 더·그림 하나 더) → '다시 요청'을 실패로 안 셈
AGAIN_OK = frozenset({"start_game", "point_game", "make_image", "make_sticker", "make_profile_video", "make_video",
                      "mention_members", "greet_members", "sports", "news_headlines", "web_search"})
COMPLAINT = re.compile(r"안\s?되(네|잖|냐|는데|노)|안\s?돼|안됨|틀렸|거짓말|아니\s?라고|왜\s?안|못\s?하(네|냐)|멍청|바보야|뭐\s?하냐|"
                       r"그게\s?아니|아니\s?그거|다시\s?해")


def gate_of(result: str) -> str:
    text = result or ""
    for gate, marks in _GATES:
        if any(m in text for m in marks):
            return gate
    return "ok"


def _load(raw, default):
    try:
        v = json.loads(raw or "")
    except (ValueError, TypeError):
        return default
    return v if isinstance(v, type(default)) else default


def steps(row) -> list[dict]:
    return [s for s in _load(row["steps"], []) if isinstance(s, dict)]


def events(row) -> list[dict]:
    try:
        raw = row["events"]
    except (KeyError, IndexError):
        return []
    return [e for e in _load(raw, []) if isinstance(e, dict)]


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", (text or "").lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


@dataclass
class Finding:
    code: str      # claim · error · soft · off · cap · redo · complaint · empty · crash
    why: str       # 한국어 한 줄

    @property
    def stage(self) -> str:
        return STAGE.get(self.code, "기타")


def analyze(row, later_runs: list | None = None, replies: list | None = None) -> list[Finding]:
    """한 실행의 실수 신호. later_runs = 같은 방·사람의 다음 실행들(REDO_SEC 안), replies = 그 사람이 답 뒤 COMPLAINT_SEC 안에
    소담에게 한 말(글 목록). 둘 다 없으면 그 신호는 안 봄."""
    out: list[Finding] = []
    st, ev = steps(row), events(row)
    answer = _col(row, "answer")
    if row["status"] == "error":
        out.append(Finding("crash", "실행 중 오류로 답을 못 함"))
    elif row["status"] == "empty":
        out.append(Finding("empty", "빈 답 (아무 말도 안 함)"))
    done = [s for s in st if s.get("w") and s.get("gate", "ok") in ("ok", "card")]
    if answer and CLAIM.search(answer) and not done:
        tried = [s["tool"] for s in st if s.get("w")]
        out.append(Finding("claim", "실제로 된 일이 없는데 '했다'고 말함"
                           + (f" (시도한 {', '.join(tried)} 은 막히거나 실패)" if tried else " (도구를 안 씀)")))
    for s in st:
        g = s.get("gate", "ok")
        if g == "error":
            out.append(Finding("error", f"{s.get('tool')} 도구 오류: {s.get('result', '')[:60]}"))
        elif g == "off":
            out.append(Finding("off", f"{s.get('tool')}: 이 방에서 꺼진 기능을 쓰려 함"))
    if st and st[-1].get("gate") == "soft" and not done:
        out.append(Finding("soft", f"{st[-1].get('tool')} 로 찾다가 못 찾고 끝남: {st[-1].get('result', '')[:60]}"))
    for e in ev:
        if e.get("e") == "cap":
            out.append(Finding("cap", {"usd": "한 번에 쓸 요금 상한에 걸려 중간에 답함",
                                       "time": "시간 상한(단톡방 25초)에 걸려 중간에 답함"}.get(e.get("kind"), "상한에 걸림")))
    trig = row["trigger"] or ""
    did_ok = any(s.get("tool") in AGAIN_OK and s.get("gate", "ok") == "ok" for s in st)
    for nxt in ([] if did_ok or trig.startswith("(이름만") else later_runs or []):
        if 0 < nxt["ts"] - row["ts"] <= REDO_SEC and similar(trig, nxt["trigger"] or "") >= 0.5:
            out.append(Finding("redo", f"같은 사람이 {max(1, (nxt['ts'] - row['ts']) // 60)}분 뒤 비슷한 요청을 다시 함 "
                                       f"(#{nxt['id']}) — 첫 답이 해결 못 한 것"))
            break
    for text in replies or []:
        if COMPLAINT.search(text or ""):
            out.append(Finding("complaint", f"답 뒤 불만: '{(text or '')[:40]}'"))
            break
    return out


def _col(row, key: str, default: str = "") -> str:
    try:
        return row[key] or default
    except (KeyError, IndexError):
        return default


def timeline(row) -> list[str]:
    """한 실행을 단계별 한국어 줄로 (Claude 가 보는 화면)."""
    lines = [f"#{row['id']} 방 {row['chat_id']} · 사람 {row['user_id']} · {row['mode']} · {row['purpose']} · {row['ms']}ms · "
             f"${row['usd_micro'] / 1e6:.4f} · 결과 {row['status']}",
             f"① 받은 말: {row['trigger']}"]
    n = 2
    circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    for e in events(row):
        k = e.get("e")
        if k == "route":
            text = f"길: {LANE_KO.get(e.get('lane'), e.get('lane'))} (신호 {e.get('why')})" + (" · 추론" if e.get("think") else "")
        elif k == "tools":
            text = f"보여 준 도구 {e.get('shown')}개 · 이 방에서 쓸 수 있는 것 {e.get('usable')}개"
        elif k == "escalate":
            text = f"작은 모델 → 큰 모델로 올려 보냄 ({e.get('why')})"
        elif k == "check":
            text = {"claim": "보내기 전 검사: 도구 없이 '했다'고 해서 한 번 다시 물음",
                    "number": "보내기 전 검사: 도구 결과에 없는 숫자라 한 번 다시 물음"}.get(e.get("kind"), str(e))
        elif k == "cap":
            text = f"상한에 걸림 ({e.get('kind')}) → 도구 없이 마무리"
        else:
            continue
        lines.append(f"{circled[min(n - 1, 19)]} {text}")
        n += 1
    for s in steps(row):
        g = s.get("gate", "ok")
        lines.append(f"{circled[min(n - 1, 19)]} 도구 {s.get('tool')}" + (" [쓰기]" if s.get("w") else "")
                     + f" ({s.get('args', '')}) → {GATE_KO.get(g, g)}: {s.get('result', '')}")
        n += 1
    answer = _col(row, "answer")
    lines.append(f"{circled[min(n - 1, 19)]} 답: {answer or '(기록 없음)'}")
    return lines
