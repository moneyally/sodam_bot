"""PR 만들기·합치기 (GitHub API, GH_TOKEN 필요).

    python pr.py create "제목" "본문"        → PR 번호 출력
    python pr.py merge 12
"""
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.getenv("SODAM_REPO", "moneyally/sodam_bot")
HEAD, BASE = os.getenv("SODAM_HEAD", "claude/button-panels"), "main"


def api(method, path, data=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 data=json.dumps(data).encode() if data else None,
                                 headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                                          "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


if __name__ == "__main__":
    if sys.argv[1] == "create":
        code, res = api("POST", "/pulls", {"title": sys.argv[2], "head": HEAD, "base": BASE, "body": sys.argv[3]})
        print(res.get("number") if code == 201 else f"실패 {code}: {res.get('message')} {res.get('errors', '')}")
    elif sys.argv[1] == "merge":
        code, res = api("PUT", f"/pulls/{sys.argv[2]}/merge", {"merge_method": "merge"})
        print(f"합침 {res.get('sha', '')[:7]}" if code == 200 else f"실패 {code}: {res.get('message')}")
