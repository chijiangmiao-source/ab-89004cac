"""Compose 内一次性验收服务。

运行顺序（任一步失败即记录并最终以非零退出码结束）：

  1. 构建检查：compileall 编译全部源码/测试；
  2. 规则测试：unittest 全套（可迁移 / 动作缺失 / 非仅起始状态比较 /
     陈旧快照 / 并发竞争 / 崩溃恢复 / HTTP 冒烟）；
  3. 对 Compose 中正在运行的 app 服务做真实 API/HTTP 冒烟，
     覆盖一组三类案例：可迁移、动作缺失、并发陈旧快照；
  4. 进程级中断恢复演练：冻结后“杀死”重开 => 未发布；
     发布后“杀死”重开 => 唯一已发布结论。

完成一次后打印汇总并以退出码报告，容器随即退出，不重启。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_URL = os.environ.get("APP_URL", "http://app:8080").rstrip("/")

OLD = {"name": "isol", "version": "v1", "start": "LOCKED", "states": {
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED", "trip": "TRIPPED"}},
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
}}
NEW = {"name": "isol", "version": "v2", "start": "LOCKED", "states": {
    k: {"output": v["output"], "actions": dict(v["actions"])}
    for k, v in OLD["states"].items()
}}
NEW_MISSING = {"name": "isol", "version": "v2", "start": "LOCKED", "states": {
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED",
                                            "force_open": "OPEN"}},
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
}}


def step(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68, flush=True)


def http(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        APP_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_for_app(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, body = http("GET", "/health")
            if status == 200 and body.get("status") == "ok":
                return True
        except Exception:
            time.sleep(0.5)
    return False


# --------------------------------------------------------------------- #
def check_build() -> bool:
    step("1/4 构建检查（compileall）")
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "tests", "verify"],
        cwd=ROOT)
    ok = proc.returncode == 0
    print("构建检查：", "通过" if ok else "失败")
    return ok


def check_unit_rules() -> bool:
    step("2/4 规则测试与持久化测试（unittest 全套）")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT)
    ok = proc.returncode == 0
    print("规则测试：", "通过" if ok else "失败")
    return ok


def check_http_smoke() -> bool:
    step(f"3/4 对运行服务 {APP_URL} 的 API/HTTP 冒烟")
    if not wait_for_app():
        print(f"无法在 30s 内连通 {APP_URL}/health")
        return False

    failures: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            failures.append(msg)

    status, body = http("GET", "/health")
    expect(status == 200 and body["status"] == "ok", "健康端点返回 ok")

    with urllib.request.urlopen(APP_URL + "/", timeout=8) as resp:
        html = resp.read().decode()
    expect("迁移裁决台" in html, "页面经 HTTP 可访问且包含裁决台")

    # --- 案例 A：可迁移，冻结后发布，结论持久可查 ---------------------
    status, rec = http("POST", "/api/decisions/freeze", {
        "migration_id": "VER-OK", "old_spec": OLD, "new_spec": NEW,
        "sessions": [
            {"session_id": "S1", "spec_version": "v1",
             "state": "LOCKED", "observed_output": "红灯"},
            {"session_id": "S2", "spec_version": "v1",
             "state": "OPEN", "observed_output": "绿灯"},
        ]})
    expect(status == 200 and rec["verdict"]["publishable"],
           "案例A 可迁移：裁决 publishable=true")
    expect(all(s["ok"] and s["target_state"] for s in rec["verdict"]["sessions"]),
           "案例A 每个会话给出目标状态")
    status, pub = http("POST", "/api/decisions/VER-OK/publish",
                       {"snapshot_hash": rec["snapshot_hash"]})
    expect(status == 200 and pub["stage"] == "PUBLISHED",
           "案例A 发布后阶段=PUBLISHED")
    status, got = http("GET", "/api/decisions/VER-OK")
    expect(status == 200 and got["stage"] == "PUBLISHED",
           "案例A 重查得到唯一已发布结论")

    # --- 案例 B：动作缺失，禁止发布，给出首个阻断路径 ---------------
    status, rec = http("POST", "/api/decisions/freeze", {
        "migration_id": "VER-MISS", "old_spec": OLD, "new_spec": NEW_MISSING,
        "sessions": [{"session_id": "S7", "spec_version": "v1",
                      "state": "OPEN", "observed_output": "绿灯"}]})
    sv = rec["verdict"]["sessions"][0]
    expect(status == 200 and not rec["verdict"]["publishable"],
           "案例B 动作缺失：publishable=false")
    expect(sv["blocking_path"] and sv["blocking_path"][-1]["action"] is None
           and sv["blocking_path"][-1]["reason"] == "action_missing_new",
           "案例B 给出首个阻断点（action_missing_new：trip）")
    status, err = http("POST", "/api/decisions/VER-MISS/publish",
                       {"snapshot_hash": rec["snapshot_hash"]})
    expect(status == 409 and err["error"] == "not_publishable",
           "案例B 阻断候选发布被拒绝（409 not_publishable）")

    # --- 案例 C：并发陈旧快照（同标识竞争/重传不得混用） -----------
    status, first = http("POST", "/api/decisions/freeze", {
        "migration_id": "VER-RACE", "old_spec": OLD, "new_spec": NEW,
        "sessions": [{"session_id": "S1", "spec_version": "v1",
                      "state": "LOCKED", "observed_output": "红灯"}]})
    expect(status == 200, "案例C 首次冻结成功")
    status, conflict = http("POST", "/api/decisions/freeze", {
        "migration_id": "VER-RACE", "old_spec": OLD, "new_spec": NEW,
        "sessions": [{"session_id": "S99", "spec_version": "v1",
                      "state": "OPEN", "observed_output": "绿灯"}]})
    expect(status == 409 and conflict["error"] == "snapshot_mismatch",
           "案例C 同标识换快照重传 => 409 snapshot_mismatch")
    status, kept = http("GET", "/api/decisions/VER-RACE")
    expect([s["session_id"] for s in kept["sessions"]] == ["S1"],
           "案例C 原会话快照未被混用覆盖")
    status, stale = http("POST", "/api/decisions/freeze", {
        "migration_id": "VER-STALE", "old_spec": OLD, "new_spec": NEW,
        "sessions": [{"session_id": "S2", "spec_version": "v1",
                      "state": "OPEN", "observed_output": "红灯"}]})
    expect(status == 400 and "快照陈旧" in stale["message"],
           "案例C 冻结瞬间观察输出不一致 => 400 快照陈旧")
    status, wrong = http("POST", "/api/decisions/VER-OK/publish",
                         {"snapshot_hash": "0" * 64})
    expect(status == 409 and wrong["error"] == "snapshot_mismatch",
           "案例C 错误快照哈希发布 => 409 拒绝")

    return not failures


def check_recovery() -> bool:
    step("4/4 进程级中断恢复演练（真实子进程重开）")

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "r.db")

        def run_subprocess(snippet: str) -> subprocess.CompletedProcess:
            script = (
                "import json, sys; sys.path.insert(0, %r); "
                "from app.core.service import DecisionService; "
                "from app.core.store import Store; "
                + snippet
            ) % ROOT
            return subprocess.run(
                [sys.executable, "-c", script, db],
                capture_output=True, text=True, timeout=30)

        # 进程 1：冻结 RC-1 后立即退出（不发布），模拟冻结后、切换前被杀
        p = run_subprocess(
            "import json,sys; "
            "OLD=%r; NEW=%r; "
            "s=Store(sys.argv[1]); svc=DecisionService(s); "
            "svc.freeze('RC-1', OLD, NEW, [{'session_id':'S1',"
            "'spec_version':'v1','state':'LOCKED','observed_output':'红灯'}]); "
            "s.close()"
            % (OLD, NEW))
        cond = p.returncode == 0
        if not cond:
            print(p.stderr)
        # 进程 2：重开同一数据库并恢复
        p2 = run_subprocess(
            "import sys; s=Store(sys.argv[1]); rolled=s.recover_interrupted(); "
            "print(json.dumps({'rolled':rolled,'gone':s.get('RC-1') is None})); "
            "s.close()")
        if p2.returncode == 0:
            out = json.loads(p2.stdout.strip().splitlines()[-1])
            cond &= out["rolled"] == ["RC-1"] and out["gone"]
        else:
            cond = False
            print(p2.stderr)
        print("  ✓ " if cond else "  ✗ ", end="")
        print("冻结后中断、重开 => 恢复为未发布")
        ok &= cond

        # 进程 3：冻结 RC-2 并发布后退出（切换完成）
        p = run_subprocess(
            "import sys; "
            "OLD=%r; NEW=%r; "
            "s=Store(sys.argv[1]); svc=DecisionService(s); "
            "r=svc.freeze('RC-2', OLD, NEW, [{'session_id':'S1',"
            "'spec_version':'v1','state':'LOCKED','observed_output':'红灯'}]); "
            "svc.publish('RC-2', r['snapshot_hash']); "
            "print(r['snapshot_hash']); s.close()"
            % (OLD, NEW))
        h = p.stdout.strip().splitlines()[-1] if p.returncode == 0 else ""
        cond = bool(h)
        if not cond:
            print(p.stderr)
        # 进程 4：重开，已发布结论须原样保留且唯一
        p2 = run_subprocess(
            "import sys,json; s=Store(sys.argv[1]); s.recover_interrupted(); "
            "r=s.get('RC-2'); "
            "print(json.dumps({'stage':r['stage'],'hash':r['snapshot_hash'],"
            "'oldver':r['old_summary']['version'],"
            "'sessions':len(r['sessions']),"
            "'publishable':r['verdict']['publishable']})); s.close()")
        if p2.returncode == 0:
            out = json.loads(p2.stdout.strip().splitlines()[-1])
            cond &= (out["stage"] == "PUBLISHED" and out["hash"] == h
                     and out["oldver"] == "v1" and out["sessions"] == 1
                     and out["publishable"] is True)
        else:
            cond = False
            print(p2.stderr)
        print("  ✓ " if cond else "  ✗ ", end="")
        print("切换后中断、重开 => 唯一已发布结论（摘要/会话/证据完整绑定）")
        ok &= cond
    return ok


def main() -> int:
    results = {
        "构建检查": check_build(),
        "规则测试": check_unit_rules(),
        "API/HTTP 冒烟": check_http_smoke(),
        "中断恢复演练": check_recovery(),
    }
    step("验收汇总")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    passed = all(results.values())
    print(f"\n一次性验收完成，退出码：{0 if passed else 1}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
