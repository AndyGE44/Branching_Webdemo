#!/usr/bin/env python3
"""Architecture A vs B benchmark for Branching_Webdemo.

Compares the cost of a snapshot/restore as a function of data size, using the
*real* classes the control plane uses:

  - **B** (data inside the checkpoint): a SQLite DB lives in the Waypoint work_dir;
    `WaypointBuildManager(build=False).snapshot()/restore()` capture it (CRIU+OverlayFS,
    fs-only in process mode). Cost tracks the whole DB file (overlayfs copies the
    file up on any write).
  - **A-cli** (external Dolt, CLI): `DoltController.snapshot()/restore()`
    (dolt add/commit/branch ; checkout+reset --hard). Content-addressed → cost
    tracks the *delta*, not total size.
  - **A-server** (external Dolt, long-lived sql-server): `DoltServerDataTier.on_snapshot()/on_restore()`
    via `CALL DOLT_*` over MySQL.

Also measures the constant app-tier Waypoint cost (empty work_dir) that A pays on
top of its Dolt op, storage growth, and steady-state point-write throughput.

Run (root, for Waypoint; from the repo root). Set DEMO_STATEFORK_ROOT if StateFork
is not at the default path. Produces docs/benchmark-arch-a-vs-b.results.json:
  sudo env PATH=$PWD/.venv/bin:/usr/local/bin:/usr/bin:/bin DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork \
    .venv/bin/python scripts/bench-arch-a-vs-b.py 1000 100000 1000000
"""
from __future__ import annotations
import csv, json, os, random, shutil, sqlite3, statistics, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, os.getenv("DEMO_STATEFORK_ROOT", "/users/alexxjk/Andy_StateFork"))
from controller import create_env_manager, DoltController          # noqa: E402
from agent_safe_demo.control_plane.dolt_server import DoltSqlServer  # noqa: E402
from agent_safe_demo.control_plane.data_tier import DoltServerDataTier  # noqa: E402

DOLT = shutil.which("dolt") or "/usr/local/bin/dolt"
SERVER_PORT = 3307
MUT_ROWS = 200          # rows dirtied before each snapshot (a realistic small delta)
random.seed(7)

def du_bytes(path: str) -> int:
    try:
        p = subprocess.run(["du", "-sb", path], capture_output=True, text=True)
        lines = (p.stdout or "").strip().splitlines()
        if lines:
            return int(lines[-1].split()[0])   # summary line for `path`
    except Exception:
        pass
    return -1

def med(xs): return round(statistics.median(xs) * 1000, 2) if xs else None
def mean(xs): return round((sum(xs) / len(xs)) * 1000, 2) if xs else None

def gen_csv(path: Path, n: int) -> None:
    locs = ["A1", "A2", "B1", "B2", "C1", "C2", "D1"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "sku", "name", "qty", "price", "location", "updated_at"])
        for i in range(1, n + 1):
            w.writerow([i, f"SKU-{i:08d}", f"Part number {i}",
                        random.randint(0, 1000), round(random.uniform(1, 999), 2),
                        random.choice(locs), "2026-06-17T00:00:00"])

DDL = ("CREATE TABLE parts (id INTEGER PRIMARY KEY, sku TEXT, name TEXT, qty INTEGER, "
       "price REAL, location TEXT, updated_at TEXT)")

def load_sqlite(db_path: Path, csv_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(DDL)
    with open(csv_path) as f:
        r = csv.reader(f); next(r)
        conn.executemany("INSERT INTO parts VALUES (?,?,?,?,?,?,?)",
                         ((int(a), b, c, int(d), float(e), g, h) for a, b, c, d, e, g, h in r))
    conn.commit(); conn.close()

def build_dolt_repo(repo: Path, csv_path: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run([DOLT, *a], cwd=repo, capture_output=True, text=True, check=True)
    run("init", "--name", "Bench", "--email", "bench@local", "--initial-branch", "main")
    run("sql", "-q", DDL.replace("INTEGER PRIMARY KEY", "INT PRIMARY KEY").replace("REAL", "DOUBLE"))
    run("table", "import", "-u", "parts", str(csv_path))
    run("add", "-A"); run("commit", "-m", "seed")

# ---------------- arch B: Waypoint fs snapshot of a SQLite DB ---------------- #
def bench_B(n: int, csv_path: Path, k: int) -> dict:
    src = Path(tempfile.mkdtemp(prefix="benchB_"))           # empty -> waypoint init
    mgr = create_env_manager("ckpt_build", dockerfile_dir=str(src), build=False)
    work = Path(mgr.work_dir)
    db = work / "data.db"
    load_sqlite(db, csv_path)                                # DB created in the overlay UPPER
    sess_base = str(work.parent)                             # /tmp/waypoint-sessions/<sid>

    def mutate():
        c = sqlite3.connect(db)
        c.execute(f"UPDATE parts SET qty = qty + 1 WHERE id <= {MUT_ROWS}"); c.commit(); c.close()

    snap_t, rest_t, sids = [], [], []
    for i in range(k + 1):                                   # +1 warmup
        mutate()
        t = time.perf_counter(); sid = mgr.snapshot(); dt = time.perf_counter() - t
        sids.append(sid)
        if i: snap_t.append(dt)
    first_upper = du_bytes(os.path.join(sess_base, sids[0], "upper"))
    for i in range(k + 1):
        sid = sids[i % len(sids)]
        t = time.perf_counter(); mgr.restore(sid); dt = time.perf_counter() - t
        if i: rest_t.append(dt)
    db_size = db.stat().st_size
    sess_total = du_bytes(sess_base)
    mgr.cleanup()
    shutil.rmtree(src, ignore_errors=True)
    subprocess.run(["sudo", "rm", "-rf", sess_base], capture_output=True)
    return {"snapshot_ms_med": med(snap_t), "snapshot_ms_mean": mean(snap_t),
            "restore_ms_med": med(rest_t), "restore_ms_mean": mean(rest_t),
            "db_bytes": db_size, "snap_upper_bytes": first_upper, "store_total_bytes": sess_total}

# ---------------- arch A-cli: DoltController -------------------------------- #
def bench_A_cli(n: int, repo: Path, k: int) -> dict:
    dc = DoltController(repo_dir=str(repo))
    def mutate():
        subprocess.run([DOLT, "sql", "-q", f"UPDATE parts SET qty = qty + 1 WHERE id <= {MUT_ROWS}"],
                       cwd=repo, capture_output=True, text=True, check=True)
    dolt_before = du_bytes(str(repo / ".dolt"))
    snap_t, rest_t, ids = [], [], []
    for i in range(k + 1):
        mutate()
        sid = f"c{i:03d}"
        t = time.perf_counter(); dc.snapshot(sid); dt = time.perf_counter() - t
        ids.append(sid)
        if i: snap_t.append(dt)
    dolt_after = du_bytes(str(repo / ".dolt"))
    for i in range(k + 1):
        sid = ids[i % len(ids)]
        t = time.perf_counter(); dc.restore(sid); dt = time.perf_counter() - t
        if i: rest_t.append(dt)
    dc.cleanup()
    return {"snapshot_ms_med": med(snap_t), "snapshot_ms_mean": mean(snap_t),
            "restore_ms_med": med(rest_t), "restore_ms_mean": mean(rest_t),
            "dolt_bytes": dolt_after, "snap_delta_bytes": dolt_after - dolt_before}

# ---------------- arch A-server: DoltServerDataTier ------------------------- #
def bench_A_server(n: int, repo: Path, k: int, throughput: bool) -> dict:
    srv = DoltSqlServer(repo, port=SERVER_PORT, dolt_bin=DOLT)
    srv.start()
    tier = DoltServerDataTier(host="127.0.0.1", port=SERVER_PORT, database=srv.database)
    import pymysql
    def conn():
        return pymysql.connect(host="127.0.0.1", port=SERVER_PORT, user="root", password="",
                               database=srv.database, autocommit=True)
    def mutate():
        c = conn()
        with c.cursor() as cur: cur.execute(f"UPDATE parts SET qty = qty + 1 WHERE id <= {MUT_ROWS}")
        c.close()
    out = {}
    snap_t, rest_t, ids = [], [], []
    try:
        for i in range(k + 1):
            mutate()
            sid = f"s{i:03d}"
            t = time.perf_counter(); tier.on_snapshot(sid); dt = time.perf_counter() - t
            ids.append(sid)
            if i: snap_t.append(dt)
        for i in range(k + 1):
            sid = ids[i % len(ids)]
            t = time.perf_counter(); tier.on_restore(sid); dt = time.perf_counter() - t
            if i: rest_t.append(dt)
        out = {"snapshot_ms_med": med(snap_t), "snapshot_ms_mean": mean(snap_t),
               "restore_ms_med": med(rest_t), "restore_ms_mean": mean(rest_t),
               "dolt_bytes": du_bytes(str(repo / ".dolt"))}
        if throughput:
            M = 3000
            c = conn()
            t = time.perf_counter()
            with c.cursor() as cur:
                for j in range(M): cur.execute(f"UPDATE parts SET qty = qty + 1 WHERE id = {j % n + 1}")
            out["throughput_ops_s"] = round(M / (time.perf_counter() - t), 1)
            c.close()
    finally:
        srv.stop()
    return out

def sqlite_throughput(csv_path: Path, n: int) -> float:
    d = Path(tempfile.mkdtemp(prefix="tput_")); db = d / "t.db"
    load_sqlite(db, csv_path)
    c = sqlite3.connect(db); c.isolation_level = None  # autocommit
    M = 3000; t = time.perf_counter()
    for j in range(M): c.execute(f"UPDATE parts SET qty = qty + 1 WHERE id = {j % n + 1}")
    r = round(M / (time.perf_counter() - t), 1)
    c.close(); shutil.rmtree(d, ignore_errors=True); return r

def cli_throughput(repo: Path, n: int) -> float:
    M = 40; t = time.perf_counter()
    for j in range(M):
        subprocess.run([DOLT, "sql", "-q", f"UPDATE parts SET qty = qty + 1 WHERE id = {j % n + 1}"],
                       cwd=repo, capture_output=True, text=True, check=True)
    return round(M / (time.perf_counter() - t), 1)

def app_tier_const(k: int = 5) -> dict:
    src = Path(tempfile.mkdtemp(prefix="apptier_"))
    mgr = create_env_manager("ckpt_build", dockerfile_dir=str(src), build=False)
    sess_base = str(Path(mgr.work_dir).parent)
    snap_t, rest_t, sids = [], [], []
    for i in range(k + 1):
        t = time.perf_counter(); sid = mgr.snapshot(); dt = time.perf_counter() - t
        sids.append(sid)
        if i: snap_t.append(dt)
    for i in range(k + 1):
        t = time.perf_counter(); mgr.restore(sids[i % len(sids)]); dt = time.perf_counter() - t
        if i: rest_t.append(dt)
    mgr.cleanup(); shutil.rmtree(src, ignore_errors=True)
    subprocess.run(["sudo", "rm", "-rf", sess_base], capture_output=True)
    return {"snapshot_ms_med": med(snap_t), "restore_ms_med": med(rest_t)}

def main():
    sizes = [int(x) for x in sys.argv[1:]] or [1000, 100000, 1000000]
    results = {"app_tier_const": app_tier_const(), "sizes": {}}
    print(f"[app-tier constant] Waypoint empty work_dir: {results['app_tier_const']}", flush=True)
    for n in sizes:
        k = 7 if n <= 100000 else 4
        print(f"\n===== size={n:,} (k={k}) =====", flush=True)
        tmp = Path(tempfile.mkdtemp(prefix=f"bench_{n}_"))
        csv_path = tmp / "data.csv"
        print("  generating csv...", flush=True); gen_csv(csv_path, n)
        base_repo = tmp / "base"
        print("  building dolt repo (import)...", flush=True); build_dolt_repo(base_repo, csv_path)
        cli_repo, srv_repo = tmp / "cli", tmp / "srv"
        shutil.copytree(base_repo, cli_repo); shutil.copytree(base_repo, srv_repo)
        row = {}
        print("  [B] waypoint+sqlite...", flush=True)
        try: row["B"] = bench_B(n, csv_path, k)
        except Exception as e: row["B"] = {"error": repr(e)}
        print("     ", row["B"], flush=True)
        print("  [A-server] dolt sql-server...", flush=True)
        try: row["A_server"] = bench_A_server(n, srv_repo, k, throughput=(n == 100000))
        except Exception as e: row["A_server"] = {"error": repr(e)}
        print("     ", row["A_server"], flush=True)
        print("  [A-cli] dolt CLI...", flush=True)
        try: row["A_cli"] = bench_A_cli(n, cli_repo, k)
        except Exception as e: row["A_cli"] = {"error": repr(e)}
        print("     ", row["A_cli"], flush=True)
        if n == 100000:
            print("  [throughput @100k]...", flush=True)
            try: row["throughput"] = {"sqlite_ops_s": sqlite_throughput(csv_path, n),
                                      "dolt_server_ops_s": row["A_server"].get("throughput_ops_s"),
                                      "dolt_cli_ops_s": cli_throughput(cli_repo, n)}
            except Exception as e: row["throughput"] = {"error": repr(e)}
            print("     ", row["throughput"], flush=True)
        results["sizes"][n] = row
        shutil.rmtree(tmp, ignore_errors=True)
        with open("/tmp/bench_results.json", "w") as f: json.dump(results, f, indent=2)
        print(f"  [saved partial results -> /tmp/bench_results.json]", flush=True)
    print("\n===== DONE =====", flush=True)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
