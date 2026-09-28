"""🔎 한국어 전문 검색 색인 (SQLite FTS5 + 글자 두 개씩 겹쳐 자르기).

SQLite FTS5 의 trigram 은 3글자 이상만 찾아서 '원두'·'환불' 같은 2음절 낱말을 못 찾는다. 그래서 검색 엔진들이
한중일 글자에 쓰는 방식(bigram)대로 낱말을 두 글자씩 겹쳐 잘라 색인하고(원두값 → 원두 두값),
검색어도 똑같이 잘라 '붙어 있는 조각'(구문)으로 찾는다 → 2글자 이상 부분 일치, 관련도(bm25) 순.
검색어는 글자·숫자만 남겨 따옴표 안에 넣으므로 FTS 문법으로 해석될 수 없다.
"""
import re
import unicodedata

_WORD = re.compile(r"[^\W_]+")


def grams(text: str) -> list[str]:
    """낱말마다 두 글자씩 겹친 조각. 한 글자 낱말은 그대로."""
    out: list[str] = []
    for w in _WORD.findall(unicodedata.normalize("NFKC", text).lower()):
        out.extend([w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)])
    return out


def index_text(text: str) -> str:
    return " ".join(grams(text))


def query_words(query: str) -> list[str]:
    """검색어의 낱말들을 색인 형태(두 글자 조각을 띄어 쓴 것)로. 2글자 미만은 뺌, 중복 제거."""
    out: list[str] = []
    for w in _WORD.findall(unicodedata.normalize("NFKC", query).lower()):
        g = " ".join(grams(w))
        if len(w) >= 2 and g not in out:
            out.append(g)
    return out


def match_query(query: str) -> tuple[str | None, int]:
    """검색어(띄어쓰기로 여러 개) → (FTS MATCH 식, 낱말 수). 찾을 게 없으면 (None, 0)."""
    words = query_words(query)
    return (" OR ".join(f'"{w}"' for w in words) or None), len(words)
