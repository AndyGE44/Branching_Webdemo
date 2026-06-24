#!/usr/bin/env python3
r"""Divergent branching snapshot/restore correctness test for the three placements.

Sequence (a real tree, not a linear chain):
  set marker=10; snapshot A
  set marker=20; snapshot B            (B child of A)
  restore A;  assert marker == 10      (rollback to A)
  set marker=30; snapshot C            (C child of A -- SIBLING of B)
  restore B;  assert marker == 20      (KEY: must be B=20, NOT C=30)
  set marker=40; snapshot D            (D child of B)
  restore C;  assert marker == 30      (C branch intact)
  restore D;  assert marker == 40      (continue-from-B branch intact)

Tree:   A -- B -- D
              \- C

marker = parts.qty WHERE id=1 (a single cell set to a known value each step, read back after each restore).
#2 also checks the in-sandbox app's HELD connection resumes at each jump (heartbeat advances, same CONNECTION_ID).

Run (root, /usr/sbin on PATH for criu; #2 needs a waypoint built with criu --file-locks --tcp-established):
  sudo env PATH=$PWD/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork .venv/bin/python scripts/test-branching.py
"""
import importlib.util, json, shutil, subprocess, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("b3", str(ROOT / "bench-three-structures.py"))
b3 = importlib.util.module_from_spec(spec); spec.loader.exec_module(b3)
create_env_manager = b3.create_env_manager
DoltSqlServer, DoltServerDataTier = b3.DoltSqlServer, b3.DoltServerDataTier
DOLT, ENVH = b3.DOLT, b3.ENVH


def mkbase(n=20):
    tmp = Path(tempfile.mkdtemp(prefix="brtest_")); csv = tmp / "data.csv"
    b3.gen_csv(csv, n); base = tmp / "base"; b3.build_repo(base, csv)
    return tmp, csv, base


# ---------------- #1 coupled: fs-only overlay, dolt CLI ----------------
def test1(base):
    mgr = create_env_manager("ckpt_build", dockerfile_dir=str(base), build=False)
    work = Path(mgr.work_dir); sess = str(work.parent)
    run = lambda *a: subprocess.run([DOLT, *a], cwd=work, env=ENVH, capture_output=True, text=True, check=True)
    def setm(v):
        run("sql", "-q", f"UPDATE parts SET qty = {v} WHERE id = 1")
        run("add", "-A"); run("commit", "-m", f"m{v}", "--allow-empty")
    def getm():
        r = run("sql", "-q", "SELECT qty FROM parts WHERE id = 1", "-r", "json")
        return int(json.loads(r.stdout)["rows"][0]["qty"])
    rows = []
    setm(10); A = mgr.snapshot()
    setm(20); B = mgr.snapshot()
    mgr.restore(A); rows.append(("restore A", 10, getm()))
    setm(30); C = mgr.snapshot()
    mgr.restore(B); rows.append(("restore B (sibling C=30)", 20, getm()))
    setm(40); D = mgr.snapshot()
    mgr.restore(C); rows.append(("restore C", 30, getm()))
    mgr.restore(D); rows.append(("restore D", 40, getm()))
    mgr.cleanup(); subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
    return rows


# ---------------- #3 external: Dolt branches version the data ----------------
def test3(base):
    import pymysql
    port = 3321
    tmp = Path(tempfile.mkdtemp(prefix="br3_")); repo = tmp / "repo"; shutil.copytree(base, repo)
    srv = DoltSqlServer(repo, port=port, dolt_bin=DOLT); srv.start()
    tier = DoltServerDataTier(host="127.0.0.1", port=port, database=srv.database)
    def conn(): return pymysql.connect(host="127.0.0.1", port=port, user="root", password="", database=srv.database, autocommit=True)
    def setm(v):
        c = conn()
        with c.cursor() as cur: cur.execute(f"UPDATE parts SET qty = {v} WHERE id = 1")
        c.close()
    def getm():
        c = conn()
        with c.cursor() as cur: cur.execute("SELECT qty FROM parts WHERE id = 1"); v = cur.fetchone()[0]
        c.close(); return int(v)
    rows = []
    setm(10); tier.on_snapshot("A")
    setm(20); tier.on_snapshot("B")
    tier.on_restore("A"); rows.append(("restore A", 10, getm()))
    setm(30); tier.on_snapshot("C")
    tier.on_restore("B"); rows.append(("restore B (sibling C=30)", 20, getm()))
    setm(40); tier.on_snapshot("D")
    tier.on_restore("C"); rows.append(("restore C", 30, getm()))
    tier.on_restore("D"); rows.append(("restore D", 40, getm()))
    tier.cleanup(); srv.stop(); shutil.rmtree(tmp, ignore_errors=True)
    return rows


# ---------------- #2 full-system: CRIU dump of server+app+conn ----------------
def test2(base, csvp):
    import pymysql
    port = 3320
    mgr = create_env_manager("ckpt_build", dockerfile_dir=b3.IMG, build=True)
    work = Path(mgr.work_dir); sess = str(work.parent)
    shutil.copy(csvp, work / "data.csv"); (work / "appclient.py").write_text(b3.APPCLIENT)
    setup = ("export HOME=/root && dolt config --global --add metrics.disabled true && "
             "mkdir -p /repo && cd /repo && dolt init --name B --email b@b --initial-branch main && "
             "dolt sql -q 'CREATE TABLE parts (id INT PRIMARY KEY, qty INT, name VARCHAR(64))' && "
             "dolt table import -u parts /data.csv && dolt add -A && dolt commit -m seed && echo OK")
    rc, out, err = mgr.exec_command(setup, timeout=300)
    if "OK" not in out:
        mgr.cleanup(); return [("setup failed: " + (err or out)[-120:], 1, 0)]
    mgr.exec_command(f"export HOME=/root GODEBUG=multipathtcp=0 && cd /repo && "
                     f"dolt sql-server --host 127.0.0.1 --port {port} >/tmp/srv.log 2>&1 & echo $!", timeout=30)
    up = False
    for _ in range(60):
        _, o, _ = mgr.exec_command("grep -qiE 'ready|accepting' /tmp/srv.log && echo UP || echo NO", timeout=10)
        if "UP" in o: up = True; break
        time.sleep(1)
    if not up:
        mgr.cleanup(); return [("server not up", 1, 0)]
    mgr.exec_command(f"export HOME=/root && nohup python3 /appclient.py {port} >/tmp/appclient.log 2>&1 & echo $!", timeout=30)
    def conn(): return pymysql.connect(host="127.0.0.1", port=port, user="root", password="", database="repo", autocommit=True, connect_timeout=2)
    def hb():
        c = conn()
        try:
            with c.cursor() as cur: cur.execute("SELECT n, cid FROM heartbeat WHERE k = 1"); return cur.fetchone()
        finally: c.close()
    def hb_retry(t=40):
        for _ in range(t):
            try:
                r = hb()
                if r: return r
            except Exception: pass
            time.sleep(0.3)
        return None
    start_cid = None
    for _ in range(60):
        try:
            r = hb()
            if r and r[0] and r[0] >= 1: start_cid = int(r[1]); break
        except Exception: pass
        time.sleep(0.5)
    if start_cid is None:
        mgr.cleanup(); subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
        return [("appclient not heartbeating", 1, 0)]
    def setm(v):
        c = conn()
        with c.cursor() as cur: cur.execute(f"UPDATE parts SET qty = {v} WHERE id = 1")
        c.close()
    def getm():
        for _ in range(40):
            try:
                c = conn()
                with c.cursor() as cur: cur.execute("SELECT qty FROM parts WHERE id = 1"); v = cur.fetchone()[0]
                c.close(); return int(v)
            except Exception: time.sleep(0.3)
        return -1
    def conncheck():          # held connection resumed? heartbeat advances on the SAME cid
        r1 = hb_retry(); time.sleep(1.0); r2 = hb_retry()
        return bool(r1 and r2 and r2[0] > r1[0] and int(r2[1]) == start_cid)
    rows = []
    setm(10); A = mgr.snapshot()
    setm(20); B = mgr.snapshot()
    mgr.restore(A); rows.append(("restore A", 10, getm(), conncheck()))
    setm(30); C = mgr.snapshot()
    mgr.restore(B); rows.append(("restore B (sibling C=30)", 20, getm(), conncheck()))
    setm(40); D = mgr.snapshot()
    mgr.restore(C); rows.append(("restore C", 30, getm(), conncheck()))
    mgr.restore(D); rows.append(("restore D", 40, getm(), conncheck()))
    mgr.cleanup(); subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
    return rows


def main():
    b3.IMG = b3.build_image()
    tmp, csv, base = mkbase()
    summary = {}
    for name, fn in [("#1 coupled (fs / dolt CLI)", lambda: test1(base)),
                     ("#3 external (Dolt branches)", lambda: test3(base)),
                     ("#2 full-system (CRIU + live conn)", lambda: test2(base, csv))]:
        print(f"\n===== {name} =====", flush=True)
        try:
            rows = fn()
        except Exception as e:
            rows = [("EXCEPTION " + repr(e)[:150], 1, 0)]
        ok_all = True
        for r in rows:
            ok = (r[1] == r[2]) and (len(r) < 4 or r[3])
            ok_all = ok_all and ok
            extra = f"  conn_resumed={r[3]}" if len(r) > 3 else ""
            print(f"  [{'PASS' if ok else 'FAIL'}] {r[0]}: expected={r[1]} got={r[2]}{extra}", flush=True)
        summary[name] = ok_all
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n===== SUMMARY =====", flush=True)
    for name, ok in summary.items():
        print(f"  {name}: {'ALL PASS' if ok else 'FAIL'}", flush=True)


if __name__ == "__main__":
    main()
