#!/usr/bin/env bash
# 뮤테이션 1개: 파일에 sed 식 적용 → 테스트 → 원래대로 복원 (경로 전체로 백업하므로 같은 이름 파일끼리 안 섞임)
#   bash mutate.sh test_news sodam/news.py 's/if score >= need:/if True:/' "기준 무시"
set -uo pipefail
test_name="$1"; file="$2"; expr="$3"; label="${4:-$3}"
bak="$(mktemp -d)/$(echo "$file" | tr '/' '_')"
cp "$file" "$bak"
sed -i "$expr" "$file"
if cmp -s "$file" "$bak"; then
  echo "NOT APPLIED <= $label"
else
  res="$(/home/user/venv/bin/python tests/run_all.py "$test_name" 2>&1 | tail -1)"
  echo "$res <= $label"
fi
cp "$bak" "$file"
