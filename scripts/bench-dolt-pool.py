import sys, time, tempfile
from pathlib import Path
sys.path.insert(0, "/users/alexxjk/Branching_Webdemo/src")
from agent_safe_demo.control_plane.dolt_server import DoltSqlServer
from agent_safe_demo.app_plane.inventory_service.store import DoltServerInventoryStore

repo = Path(tempfile.mkdtemp(prefix="pool_bench_")) / "inv"
srv = DoltSqlServer(repo, port=3307, dolt_bin="dolt")
srv.start()
db = srv.database
print("server db =", db, flush=True)

def make(pool):
    return DoltServerInventoryStore(host="127.0.0.1", port=3307, database=db, pool_size=pool)

init_store = make(5)
init_store.init()
pid = init_store.inventory_items()[0]["id"]
print("seeded; point-write target id =", pid, flush=True)

def t_update(store, M):
    store._execute("UPDATE parts SET on_hand = on_hand + 1 WHERE id = %s", (pid,))  # warmup
    t = time.perf_counter()
    for _ in range(M):
        store._execute("UPDATE parts SET on_hand = on_hand + 1 WHERE id = %s", (pid,))
    return round(M / (time.perf_counter() - t), 1)

def t_buy(store, M):
    store.buy(pid, 1, "bench")  # warmup
    t = time.perf_counter()
    for _ in range(M):
        store.buy(pid, 1, "bench")
    return round(M / (time.perf_counter() - t), 1)

res = {}
for pool in (0, 5):
    s = make(pool)
    res[pool] = {"single_update_ops_s": t_update(s, 2000), "buy_request_ops_s": t_buy(s, 800)}
    if pool:
        s.close_pool()
    print(f"pool_size={pool}: {res[pool]}", flush=True)
srv.stop()

u0, u5 = res[0]["single_update_ops_s"], res[5]["single_update_ops_s"]
b0, b5 = res[0]["buy_request_ops_s"], res[5]["buy_request_ops_s"]
print(f"\nsingle UPDATE: per-query {u0} -> pooled {u5}  ({u5/u0:.1f}x)", flush=True)
print(f"buy() request: per-query {b0} -> pooled {b5}  ({b5/b0:.1f}x)", flush=True)
import json
Path("/tmp/bench_pool_results.json").write_text(json.dumps(res, indent=2))
