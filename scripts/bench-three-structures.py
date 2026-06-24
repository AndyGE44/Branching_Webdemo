#!/usr/bin/env python3
"""Three placements of an internal/external Dolt data tier, snapshot/restore + storage.

#1 coupled    : Dolt repo files live inside the Waypoint checkpoint; fs-only snapshot
                versions them (no DB process). Versioning = StateFork fs (Waypoint).
#2 full-system: dolt sql-server runs INSIDE the sandbox; CRIU checkpoints the server's
                memory + the repo together. Versioning = StateFork checkpoint (CRIU+fs).
#3 external(A): dolt sql-server OUTSIDE; Dolt's own commit/branch/reset versions the data;
                StateFork only checkpoints the (tiny) app. Versioning = Dolt branches.

Run (root, /usr/sbin on PATH for criu):
  sudo env PATH=.../venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin venv/bin/python bench3.py
"""
from __future__ import annotations
import csv, json, os, random, shutil, statistics, subprocess, sys, tempfile, time, uuid
from pathlib import Path
sys.path.insert(0, os.getenv("DEMO_STATEFORK_ROOT", "/users/alexxjk/Andy_StateFork"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from controller import create_env_manager
from agent_safe_demo.control_plane.dolt_server import DoltSqlServer
from agent_safe_demo.control_plane.data_tier import DoltServerDataTier

DOLT = shutil.which("dolt") or "/usr/local/bin/dolt"
MUT = 200
random.seed(7)
ENVH = {**os.environ, "HOME": "/root"}          # host dolt needs a real HOME under sudo
IMG = None                                        # sandbox image dir; built by build_image()

# In-sandbox app client for #2: opens ONE connection to the co-located dolt sql-server and
# heartbeats on it forever (reconnect OFF). When CRIU dumps the whole session, this process
# and its established loopback connection are captured WITH the server. After a restore the
# loop keeps writing -- so an external reader seeing `n` advance while `cid` (the server-side
# CONNECTION_ID) stays constant proves the exact same connection survived, with no reconnect.
APPCLIENT = r'''
import sys, time, pymysql
port = int(sys.argv[1])
conn = pymysql.connect(host="127.0.0.1", port=port, user="root", password="",
                       database="repo", autocommit=True, connect_timeout=5)
def q1(sql):
    with conn.cursor() as cur:
        cur.execute(sql); row = cur.fetchone()
        return row[0] if row else None
start_cid = q1("SELECT CONNECTION_ID()")
with conn.cursor() as cur:
    cur.execute("CREATE TABLE IF NOT EXISTS heartbeat (k INT PRIMARY KEY, n BIGINT, cid BIGINT)")
    cur.execute("DELETE FROM heartbeat WHERE k = 1")
    cur.execute("INSERT INTO heartbeat (k, n, cid) VALUES (1, 0, %s)", (start_cid,))
n = 0
while True:
    try:
        n += 1
        cid = q1("SELECT CONNECTION_ID()")          # raises (reconnect OFF) if the socket died
        with conn.cursor() as cur:
            cur.execute("UPDATE heartbeat SET n = %s, cid = %s WHERE k = 1", (n, cid))
    except Exception:
        time.sleep(0.2)
    time.sleep(0.3)
'''

# In-sandbox throughput client for #2: reuse ONE connection to the co-located server and time
# N point-writes, then print "OPS <ops/s>". Run via exec_command so the measurement happens
# from inside the sandbox (in-sandbox client -> in-sandbox server), the faithful #2 path.
TPUTCLIENT = r'''
import sys, time, pymysql
port = int(sys.argv[1]); n = int(sys.argv[2])
conn = pymysql.connect(host="127.0.0.1", port=port, user="root", password="",
                       database="repo", autocommit=True)
cur = conn.cursor()
cur.execute("UPDATE parts SET qty = qty + 1 WHERE id = 1")    # warmup
t = time.perf_counter()
for _ in range(n):
    cur.execute("UPDATE parts SET qty = qty + 1 WHERE id = 1")
print("OPS %.3f" % (n / (time.perf_counter() - t)))
'''


def build_image():
    """Self-contained Waypoint build context: python:3.12-slim + the host's dolt binary,
    with a commit identity baked in. (#2 runs dolt sql-server inside this sandbox.)"""
    # waypoint derives a buildah tag `waypoint_<dirname>:<ts>`, so the build-context basename
    # must be a valid image-reference component (lowercase alnum, no trailing separator).
    # mkdtemp's random suffix can end in '_' -> "invalid reference format"; name the dir ourselves.
    d = Path(tempfile.mkdtemp()) / f"img3{uuid.uuid4().hex[:10]}"
    d.mkdir()
    shutil.copy(DOLT, d / "dolt")
    (d / "Dockerfile").write_text(
        "FROM python:3.12-slim\nWORKDIR /\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends procps ca-certificates "
        "&& rm -rf /var/lib/apt/lists/*\n"
        "COPY dolt /usr/local/bin/dolt\n"
        "RUN chmod +x /usr/local/bin/dolt "
        "&& dolt config --global --add user.name Bench && dolt config --global --add user.email bench@local\n"
        "RUN pip install --no-cache-dir pymysql\n"   # #2's in-sandbox app client holds a warm conn
        'CMD ["bash"]\n'
    )
    return str(d)


def du(p):
    try:
        r = subprocess.run(["du", "-sb", p], capture_output=True, text=True)
        ls = (r.stdout or "").strip().splitlines()
        return int(ls[-1].split()[0]) if ls else -1
    except Exception:
        return -1

def med(xs): return round(statistics.median(xs) * 1000, 1) if xs else None

def gen_csv(path, n):
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["id", "qty", "name"])
        for i in range(1, n + 1):
            w.writerow([i, random.randint(0, 1000), f"part {i}"])

def build_repo(repo, csv_path):
    repo.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run([DOLT, *a], cwd=repo, env=ENVH, capture_output=True, text=True, check=True)
    run("init", "--name", "B", "--email", "b@b", "--initial-branch", "main")
    run("sql", "-q", "CREATE TABLE parts (id INT PRIMARY KEY, qty INT, name VARCHAR(64))")
    run("table", "import", "-u", "parts", str(csv_path))
    run("add", "-A"); run("commit", "-m", "seed")

# ---------- #1 coupled: Dolt files inside a fs-only Waypoint checkpoint ---------- #
def struct1(base_repo, k):
    # base repo is the overlay LOWER (read-only); the commit's new chunk files land in the
    # UPPER, so the snapshot's upper = the per-snapshot delta (not the whole repo).
    mgr = create_env_manager("ckpt_build", dockerfile_dir=str(base_repo), build=False)
    work = Path(mgr.work_dir); sess = str(work.parent)
    run = lambda *a: subprocess.run([DOLT, *a], cwd=work, env=ENVH, capture_output=True, text=True, check=True)
    def mutate():
        run("sql", "-q", f"UPDATE parts SET qty = qty + 1 WHERE id <= {MUT}")
        run("add", "-A"); run("commit", "-m", "d", "--allow-empty")
    snap, rest, sids = [], [], []
    for i in range(k + 1):
        mutate()
        t = time.perf_counter(); sid = mgr.snapshot(); dt = time.perf_counter() - t
        sids.append(sid)
        if i: snap.append(dt)
    upper = du(os.path.join(sess, sids[0], "upper"))
    for i in range(k + 1):
        t = time.perf_counter(); mgr.restore(sids[i % len(sids)])
        if i: rest.append(time.perf_counter() - t)
    whole = du(str(base_repo / ".dolt"))
    mgr.cleanup()
    subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
    return {"snap_ms": med(snap), "rest_ms": med(rest), "criu_bytes": 0,
            "fs_upper_bytes": upper, "whole_repo_bytes": whole}

# ---------- #1b coupled in BUILD mode: same sandbox+CRIU as #2, but NO DB server ---------- #
def struct1_build(base_repo, csv_path, k):
    # Fair control vs #2: identical build-mode CRIU machinery (sandbox + waypoint create with
    # memory), but no running dolt server -- only the idle shell. So criu ~= build-mode
    # overhead; the gap vs #2 is exactly the server's memory.
    mgr = create_env_manager("ckpt_build", dockerfile_dir=IMG, build=True)
    work = Path(mgr.work_dir); sess = str(work.parent)
    shutil.copy(csv_path, work / "data.csv")
    setup = ("export HOME=/root && dolt config --global --add metrics.disabled true && "
             "mkdir -p /repo && cd /repo && dolt init --name B --email b@b --initial-branch main && "
             "dolt sql -q 'CREATE TABLE parts (id INT PRIMARY KEY, qty INT, name VARCHAR(64))' && "
             "dolt table import -u parts /data.csv && dolt add -A && dolt commit -m seed && echo OK")
    rc, out, err = mgr.exec_command(setup, timeout=300)
    if "OK" not in out:
        mgr.cleanup(); return {"error": "setup: " + (err or out)[-200:]}
    def mutate():
        mgr.exec_command("export HOME=/root && cd /repo && "
                         f"dolt sql -q 'UPDATE parts SET qty = qty + 1 WHERE id <= {MUT}' && "
                         "dolt add -A && dolt commit -m d --allow-empty", timeout=120)
    snap, rest, sids = [], [], []
    for i in range(k + 1):
        mutate()
        t = time.perf_counter(); sid = mgr.snapshot(); dt = time.perf_counter() - t
        if sid is None:
            mgr.cleanup(); return {"error": "snapshot failed"}
        sids.append(sid)
        if i: snap.append(dt)
    criu = du(os.path.join(sess, sids[0], "criu")); upper = du(os.path.join(sess, sids[0], "upper"))
    for i in range(k + 1):
        t = time.perf_counter(); mgr.restore(sids[i % len(sids)])
        if i: rest.append(time.perf_counter() - t)
    mgr.cleanup(); subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
    return {"snap_ms": med(snap), "rest_ms": med(rest), "criu_bytes": criu, "fs_upper_bytes": upper}

# ---------- #2 full-system: dolt sql-server + app client INSIDE the sandbox, CRIU all ---------- #
def struct2(base_repo, csv_path, k, port=3308):
    # Faithful full-system: BOTH the dolt sql-server AND an app client holding a warm connection
    # live inside the sandbox, so CRIU captures the server, the app, and the established (loopback)
    # connection between them. waypoint passes `criu --tcp-established`, so the connection is
    # dumped/restored on both ends -- restore needs NO reconnect, which we assert below via the
    # heartbeat the app keeps writing on that held connection. The data-dirtying 200-row UPDATE is
    # still host-driven (closed before each snapshot) so the per-snapshot delta stays identical to
    # the other structures; the only added cost vs a server-only #2 is the app + its live socket.
    import pymysql
    mgr = create_env_manager("ckpt_build", dockerfile_dir=IMG, build=True)
    work = Path(mgr.work_dir); sess = str(work.parent)
    shutil.copy(csv_path, work / "data.csv")                       # host work_dir == sandbox /
    (work / "appclient.py").write_text(APPCLIENT)
    setup = ("export HOME=/root && dolt config --global --add metrics.disabled true && "
             "mkdir -p /repo && cd /repo && dolt init --name B --email b@b --initial-branch main && "
             "dolt sql -q 'CREATE TABLE parts (id INT PRIMARY KEY, qty INT, name VARCHAR(64))' && "
             "dolt table import -u parts /data.csv && dolt add -A && dolt commit -m seed && echo OK")
    rc, out, err = mgr.exec_command(setup, timeout=300)
    if "OK" not in out:
        mgr.cleanup(); return {"error": "setup: " + (err or out)[-200:]}
    mgr.exec_command(f"export HOME=/root GODEBUG=multipathtcp=0 && cd /repo && "
                     f"dolt sql-server --host 127.0.0.1 --port {port} >/tmp/srv.log 2>&1 & echo $!", timeout=30)
    up = False
    for _ in range(60):
        _, o, _ = mgr.exec_command("grep -qiE 'ready|accepting' /tmp/srv.log && echo UP || echo NO", timeout=10)
        if "UP" in o: up = True; break
        time.sleep(1)
    if not up:
        mgr.cleanup(); return {"error": "server not up"}
    # in-sandbox app: open ONE warm connection and heartbeat on it for the whole run
    mgr.exec_command(f"export HOME=/root && nohup python3 /appclient.py {port} "
                     f">/tmp/appclient.log 2>&1 & echo $!", timeout=30)
    def hb():                                  # read the heartbeat via a separate short connection
        c = pymysql.connect(host="127.0.0.1", port=port, user="root", password="",
                            database="repo", connect_timeout=2)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT n, cid FROM heartbeat WHERE k = 1")
                return cur.fetchone()          # (n, cid) or None
        finally:
            c.close()
    def hb_retry(tries=40):                    # tolerate the server briefly relisten-ing post-restore
        for _ in range(tries):
            try:
                row = hb()
                if row: return row
            except Exception:
                pass
            time.sleep(0.3)
        return None
    start_cid = None
    for _ in range(60):
        try:
            row = hb()
            if row and row[0] and row[0] >= 1:
                start_cid = int(row[1]); break
        except Exception:
            pass
        time.sleep(0.5)
    if start_cid is None:
        log = mgr.exec_command("tail -3 /tmp/appclient.log 2>/dev/null", timeout=10)[1]
        mgr.cleanup(); return {"error": "app client not heartbeating: " + (log or "")[-160:]}
    def mutate():
        c = pymysql.connect(host="127.0.0.1", port=port, user="root", password="", database="repo", autocommit=True)
        with c.cursor() as cur: cur.execute(f"UPDATE parts SET qty = qty + 1 WHERE id <= {MUT}")
        c.close()
    snap, rest, sids = [], [], []
    for i in range(k + 1):
        mutate()
        t = time.perf_counter(); sid = mgr.snapshot(); dt = time.perf_counter() - t
        if sid is None:
            mgr.cleanup(); return {"error": "snapshot failed"}
        sids.append(sid)
        if i: snap.append(dt)
    criu = du(os.path.join(sess, sids[0], "criu")); upper = du(os.path.join(sess, sids[0], "upper"))
    # prove the app's held connection survives a restore with NO reconnect: after restoring, the
    # heartbeat must keep advancing (n up) on the SAME server-side session (cid == start_cid).
    conn_survived = cid_stable = False; proof = {}
    try:
        mgr.restore(sids[-1])
        r1 = hb_retry(); time.sleep(1.3); r2 = hb_retry()
        if r1 and r2:
            conn_survived = bool(r2[0] > r1[0])
            cid_stable = bool(int(r2[1]) == start_cid)
            proof = {"start_cid": start_cid, "cid_after": int(r2[1]),
                     "hb_before": int(r1[0]), "hb_after": int(r2[0])}
        else:
            proof = {"error": "no heartbeat after restore", "r1": r1, "r2": r2}
    except Exception as e:
        proof = {"error": repr(e)[:140]}
    for i in range(k + 1):
        t = time.perf_counter(); mgr.restore(sids[i % len(sids)])
        if i: rest.append(time.perf_counter() - t)
    mgr.cleanup(); subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
    return {"snap_ms": med(snap), "rest_ms": med(rest), "criu_bytes": criu, "fs_upper_bytes": upper,
            "conn_survived": conn_survived, "cid_stable": cid_stable, "conn_proof": proof}

# ---------- #3 external (arch A): Dolt branches + tiny app checkpoint ---------- #
def app_tier_const(k=5):
    src = Path(tempfile.mkdtemp(prefix="app_"))
    mgr = create_env_manager("ckpt_build", dockerfile_dir=str(src), build=False)
    sess = str(Path(mgr.work_dir).parent); snap, rest, sids = [], [], []
    for i in range(k + 1):
        t = time.perf_counter(); sid = mgr.snapshot();
        sids.append(sid)
        if i: snap.append(time.perf_counter() - t)
    for i in range(k + 1):
        t = time.perf_counter(); mgr.restore(sids[i % len(sids)])
        if i: rest.append(time.perf_counter() - t)
    mgr.cleanup(); shutil.rmtree(src, ignore_errors=True)
    subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
    return med(snap), med(rest)

def struct3(base_repo, k, app_snap, app_rest, port=3309):
    repo = Path(tempfile.mkdtemp(prefix="s3_")) / "repo"; shutil.copytree(base_repo, repo)
    srv = DoltSqlServer(repo, port=port, dolt_bin=DOLT); srv.start()
    tier = DoltServerDataTier(host="127.0.0.1", port=port, database=srv.database)
    import pymysql
    def mutate():
        c = pymysql.connect(host="127.0.0.1", port=port, user="root", password="", database=srv.database, autocommit=True)
        with c.cursor() as cur: cur.execute(f"UPDATE parts SET qty = qty + 1 WHERE id <= {MUT}")
        c.close()
    before = du(str(repo / ".dolt")); snap, rest, ids = [], [], []
    for i in range(k + 1):
        mutate(); sid = f"s{i:03d}"
        t = time.perf_counter(); tier.on_snapshot(sid); dt = time.perf_counter() - t
        ids.append(sid)
        if i: snap.append(dt)
    after = du(str(repo / ".dolt"))
    for i in range(k + 1):
        t = time.perf_counter(); tier.on_restore(ids[i % len(ids)])
        if i: rest.append(time.perf_counter() - t)
    srv.stop(); shutil.rmtree(repo.parent, ignore_errors=True)
    # end-to-end = Dolt op + the (tiny) app-tier Waypoint op
    return {"snap_ms": round(med(snap) + app_snap, 1), "rest_ms": round(med(rest) + app_rest, 1),
            "dolt_op_snap_ms": med(snap), "criu_bytes": 0,
            "fs_upper_bytes": 0, "data_delta_bytes": after - before}

# ---------- #3 external in BUILD mode: app (idle shell) in a CRIU sandbox; Dolt external ---------- #
def struct3_build(base_repo, k, port=3310):
    # Same build-mode CRIU as #1-build/#2, but the dolt server is EXTERNAL and Dolt's own
    # branches version the data. The checkpoint captures the app's memory (idle shell) + tiny
    # fs; the data stays external (delta), so the checkpoint never grows with data size.
    import pymysql
    repo = Path(tempfile.mkdtemp(prefix="s3b_")) / "repo"; shutil.copytree(base_repo, repo)
    srv = DoltSqlServer(repo, port=port, dolt_bin=DOLT); srv.start()
    tier = DoltServerDataTier(host="127.0.0.1", port=port, database=srv.database)
    mgr = create_env_manager("ckpt_build", dockerfile_dir=IMG, build=True)   # idle-shell sandbox, no repo inside
    sess = str(Path(mgr.work_dir).parent)
    def mutate():
        c = pymysql.connect(host="127.0.0.1", port=port, user="root", password="", database=srv.database, autocommit=True)
        with c.cursor() as cur: cur.execute(f"UPDATE parts SET qty = qty + 1 WHERE id <= {MUT}")
        c.close()
    before = du(str(repo / ".dolt")); snap, rest, ids = [], [], []
    for i in range(k + 1):
        mutate(); did = f"s{i:03d}"
        t = time.perf_counter(); ck = mgr.snapshot(); tier.on_snapshot(did); dt = time.perf_counter() - t
        if ck is None:
            mgr.cleanup(); srv.stop(); return {"error": "snapshot failed"}
        ids.append((ck, did))
        if i: snap.append(dt)
    criu = du(os.path.join(sess, ids[0][0], "criu")); upper = du(os.path.join(sess, ids[0][0], "upper"))
    after = du(str(repo / ".dolt"))
    for i in range(k + 1):
        ck, did = ids[i % len(ids)]
        t = time.perf_counter(); mgr.restore(ck); tier.on_restore(did)
        if i: rest.append(time.perf_counter() - t)
    mgr.cleanup(); srv.stop()
    subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True); shutil.rmtree(repo.parent, ignore_errors=True)
    return {"snap_ms": med(snap), "rest_ms": med(rest), "criu_bytes": criu,
            "fs_upper_bytes": upper, "data_delta_bytes": after - before}

# ---------- point-write throughput by data-access method ---------- #
def throughput(base_repo, csv_path, n_cli=40, n_srv=2000, p_host=3311, p_sbx=3312):
    """Three REAL point-write measurements, one per placement (no shared/borrowed number):
       #1 coupled    : dolt CLI -- a fresh `dolt` process per query, no server.
       #2 full-system: dolt sql-server INSIDE the sandbox, timed by an IN-SANDBOX client.
       #3 external   : dolt sql-server on the HOST, timed by a host client (reused conn).
    All are PK point writes (id=1), ~size-independent; #2/#3 reuse one pooled connection."""
    import pymysql
    tmp = Path(tempfile.mkdtemp(prefix="tput_"))
    # ---- #1 coupled: dolt CLI, a process per query ----
    cli_repo = tmp / "cli"; shutil.copytree(base_repo, cli_repo)
    t = time.perf_counter()
    for _ in range(n_cli):
        subprocess.run([DOLT, "sql", "-q", "UPDATE parts SET qty = qty + 1 WHERE id = 1"],
                       cwd=cli_repo, env=ENVH, capture_output=True, text=True, check=True)
    s1 = round(n_cli / (time.perf_counter() - t), 1)
    # ---- #3 external: host dolt sql-server, reused (pooled) connection ----
    srv_repo = tmp / "srv"; shutil.copytree(base_repo, srv_repo)
    srv = DoltSqlServer(srv_repo, port=p_host, dolt_bin=DOLT); srv.start()
    c = pymysql.connect(host="127.0.0.1", port=p_host, user="root", password="", database=srv.database, autocommit=True)
    with c.cursor() as cur:
        cur.execute("UPDATE parts SET qty = qty + 1 WHERE id = 1")   # warmup
        t = time.perf_counter()
        for _ in range(n_srv):
            cur.execute("UPDATE parts SET qty = qty + 1 WHERE id = 1")
        s3 = round(n_srv / (time.perf_counter() - t), 1)
    c.close(); srv.stop()
    # ---- #2 full-system: dolt sql-server INSIDE the sandbox, in-sandbox client ----
    s2 = None; s2_err = None
    mgr = create_env_manager("ckpt_build", dockerfile_dir=IMG, build=True)
    sess = str(Path(mgr.work_dir).parent)
    try:
        work = Path(mgr.work_dir)
        shutil.copy(csv_path, work / "data.csv")
        (work / "tput_client.py").write_text(TPUTCLIENT)
        setup = ("export HOME=/root && dolt config --global --add metrics.disabled true && "
                 "mkdir -p /repo && cd /repo && dolt init --name B --email b@b --initial-branch main && "
                 "dolt sql -q 'CREATE TABLE parts (id INT PRIMARY KEY, qty INT, name VARCHAR(64))' && "
                 "dolt table import -u parts /data.csv && dolt add -A && dolt commit -m seed && echo OK")
        rc, out, err = mgr.exec_command(setup, timeout=300)
        if "OK" not in out:
            s2_err = "setup: " + (err or out)[-160:]
        else:
            mgr.exec_command(f"export HOME=/root GODEBUG=multipathtcp=0 && cd /repo && "
                             f"dolt sql-server --host 127.0.0.1 --port {p_sbx} >/tmp/srv.log 2>&1 & echo $!", timeout=30)
            up = False
            for _ in range(60):
                _, o, _ = mgr.exec_command("grep -qiE 'ready|accepting' /tmp/srv.log && echo UP || echo NO", timeout=10)
                if "UP" in o: up = True; break
                time.sleep(1)
            if not up:
                s2_err = "server not up"
            else:
                rc, out, err = mgr.exec_command(f"python3 /tput_client.py {p_sbx} {n_srv}", timeout=180)
                line = next((ln for ln in (out or "").splitlines() if ln.startswith("OPS")), None)
                if line: s2 = round(float(line.split()[1]), 1)
                else: s2_err = "no OPS: " + ((err or out) or "")[-160:]
    finally:
        mgr.cleanup()
        subprocess.run(["sudo", "rm", "-rf", sess], capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
    res = {"s1_cli_ops_s": s1, "s2_insandbox_pooled_ops_s": s2, "s3_external_pooled_ops_s": s3,
           "dolt_cli_ops_s": s1, "dolt_server_pooled_ops_s": s3}   # legacy keys for old readers
    if s2_err: res["s2_error"] = s2_err
    return res

def main():
    argv = sys.argv[1:]
    global IMG
    # `throughput-only [rows]`: just the 3-placement point-write benchmark (size-independent),
    # written to /tmp/bench3_throughput.json -- no need to rerun the latency/storage suite.
    if argv and argv[0] == "throughput-only":
        IMG = build_image()
        rows = int(argv[1]) if len(argv) > 1 else 1000
        tmp = Path(tempfile.mkdtemp(prefix="tputonly_")); csvp = tmp / "data.csv"
        gen_csv(csvp, rows); base = tmp / "base"; build_repo(base, csvp)
        print(f"[throughput-only] rows={rows}", flush=True)
        res = throughput(base, csvp)
        with open("/tmp/bench3_throughput.json", "w") as f: json.dump(res, f, indent=2)
        shutil.rmtree(tmp, ignore_errors=True)
        print(json.dumps(res, indent=2), flush=True)
        return
    sizes = [int(x) for x in argv] or [1000, 100000, 1000000]
    k = 3
    IMG = build_image()
    a_snap, a_rest = app_tier_const()
    res = {"app_tier_const_ms": {"snap": a_snap, "rest": a_rest}, "sizes": {}}
    print(f"[app-tier const] snap={a_snap} rest={a_rest}", flush=True)
    for n in sizes:
        print(f"\n===== size={n:,} =====", flush=True)
        tmp = Path(tempfile.mkdtemp(prefix=f"b3_{n}_")); csvp = tmp / "data.csv"
        gen_csv(csvp, n); base = tmp / "base"; build_repo(base, csvp)
        row = {}
        for name, fn in [("s1_coupled", lambda: struct1(base, k)),
                         ("s1_build", lambda: struct1_build(base, csvp, k)),
                         ("s3_external", lambda: struct3(base, k, a_snap, a_rest)),
                         ("s3_build", lambda: struct3_build(base, k)),
                         ("s2_fullsystem", lambda: struct2(base, csvp, k))]:
            print(f"  [{name}] ...", flush=True)
            try: row[name] = fn()
            except Exception as e: row[name] = {"error": repr(e)[:200]}
            print(f"     {row[name]}", flush=True)
        res["sizes"][n] = row
        if n == sizes[0]:
            print("  [throughput] ...", flush=True)
            try: res["throughput"] = throughput(base, csvp)
            except Exception as e: res["throughput"] = {"error": repr(e)[:200]}
            print(f"     {res.get('throughput')}", flush=True)
        shutil.rmtree(tmp, ignore_errors=True)
        with open("/tmp/bench3_results.json", "w") as f: json.dump(res, f, indent=2)
    print("\nDONE\n" + json.dumps(res, indent=2), flush=True)

if __name__ == "__main__":
    main()
