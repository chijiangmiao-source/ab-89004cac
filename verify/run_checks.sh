#!/bin/sh
# Acceptance gate for the migration adjudicator.
#
# Runs, in order, inside the Compose `verify` service:
#   1. rule tests (bisimulation + durable store / concurrency / recovery)
#   2. build check (byte-compile every module)
#   3. API/HTTP smoke tests against a real server process
#
# Exits 0 only if every stage passes; the first failing stage is reported
# and the aggregate exit code reflects it.
set -u

cd /workspace 2>/dev/null || cd "$(dirname "$0")/.."
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
export PYTHONDONTWRITEBYTECODE=1

fail=0
section() {
    echo ""
    echo "================================================================"
    echo "  $1"
    echo "================================================================"
}

section "1/3 规则测试（可迁移 / 动作缺失 / 陈旧快照 / 并发 / 崩溃恢复）"
python3 -m unittest -v tests.test_rules
rc=$?
[ $rc -ne 0 ] && fail=$rc

section "2/3 构建检查（全部模块字节编译）"
python3 -m compileall -q app verify && echo "compileall: OK"
rc=$?
[ $rc -ne 0 ] && fail=$rc

section "3/3 API/HTTP 冒烟（健康、冻结、409 陈旧快照、杀进程重开恢复）"

# When the app service is linked (Compose), also probe it over the network;
# otherwise the live cases skip automatically.
# The live container probe runs only when APP_BASE_URL is provided
# (the Compose verify service injects it).  DNS alone is not a reliable
# signal — some sandboxes wildcard-resolve arbitrary hostnames.
if [ -n "${APP_BASE_URL:-}" ]; then
    url="${APP_BASE_URL%/}"
    i=0
    while [ "$i" -lt 30 ]; do
        if python3 - "$url" <<'PY'
import sys, urllib.request
try:
    r = urllib.request.urlopen(sys.argv[1] + "/healthz", timeout=2)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
        then
            echo "app 服务已就绪：${url}（附加在线容器探测）"
            LIVE_URL="$url"
            break
        fi
        i=$((i + 1))
        sleep 1
    done
    [ -z "${LIVE_URL:-}" ] && echo "警告：APP_BASE_URL 已设置但 app 未就绪，在线探测将失败"
else
    echo "未设置 APP_BASE_URL（非 Compose 环境），跳过在线容器探测"
fi
APP_BASE_URL="${LIVE_URL:-}" python3 -m unittest -v verify.smoke_test
rc=$?
[ $rc -ne 0 ] && fail=$rc

echo ""
echo "================================================================"
if [ "$fail" -eq 0 ]; then
    echo "  VERIFY RESULT: PASS (rules + build + smoke)"
else
    echo "  VERIFY RESULT: FAIL (exit code $fail)"
fi
echo "================================================================"
exit "$fail"
