#!/usr/bin/env bash
# 部署后冒烟自检（不消耗模型额度，除最后一步可选用例）
set -uo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"
fail=0
chk(){ printf '  %-42s' "$1"; if eval "$2" >/dev/null 2>&1; then echo "OK"; else echo "FAIL"; fail=1; fi; }

echo "== 冒烟自检 $BASE =="
chk "健康检查 /healthz"            "curl -fsS $BASE/healthz"
chk "管理前端 /"                    "curl -fsS $BASE/ | grep -q PromptQL"
chk "状态接口 /api/state"           "curl -fsS $BASE/api/state"
chk "5sim 连通 /api/fivesim/balance" "curl -fsS $BASE/api/fivesim/balance | grep -q '\"ok\":true'"

KEY="${SMOKE_KEY:-}"
if [ -n "$KEY" ]; then
  echo "== 网关用例（会消耗额度）=="
  chk "chat/completions" "curl -fsS -m 240 $BASE/v1/chat/completions \
      -H 'Authorization: Bearer $KEY' -H 'Content-Type: application/json' \
      -d '{\"model\":\"claude-fable-5-1\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}]}'"
else
  echo "  (跳过网关用例；设 SMOKE_KEY=<下游Key> 可启用)"
fi

[ "$fail" -eq 0 ] && echo "== 全部通过 ==" || { echo "== 有失败项 =="; exit 1; }
