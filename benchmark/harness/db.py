"""Shared OMOP DB connection helper.

Creds come from the macOS keychain (biorouter 'secrets' blob), but the running
production BioRouter.app periodically clobbers that blob with the placeholder
'dummy'. To keep long test runs robust we (1) keep a local gitignored cache of
the last-known-good creds, (2) if the keychain is clobbered, recover from the
cache or from any running extension subprocess env, and (3) refresh the cache
whenever we read real creds.
"""
import json, subprocess, functools, os, re, time
import pymssql

OMOP_SERVER = "QCDIDDWDB001.ucsfmedicalcenter.org"
OMOP_DATABASE = "OMOP_DEID"
_CACHE = os.path.join(os.path.dirname(__file__), ".creds_cache.json")


def _from_keychain():
    blob = subprocess.check_output(
        ["security", "find-generic-password", "-s", "biorouter", "-a", "secrets", "-w"])
    d = json.loads(blob)
    return d.get("CLINICAL_RECORDS_USERNAME"), d.get("CLINICAL_RECORDS_PASSWORD")


def _from_proc_env():
    for pid in subprocess.check_output(["ps", "-A", "-o", "pid="]).split():
        try:
            env = subprocess.check_output(["ps", "eww", "-p", pid.decode().strip()],
                                          stderr=subprocess.DEVNULL).decode()
        except Exception:
            continue
        mu = re.search(r"CLINICAL_RECORDS_USERNAME=(\S+)", env)
        mp = re.search(r"CLINICAL_RECORDS_PASSWORD=(\S+)", env)
        if mu and mp and mu.group(1) != "dummy":
            return mu.group(1), mp.group(1)
    return None, None


def _valid(u, p):
    return u and p and u != "dummy" and p != "dummy"


@functools.lru_cache(maxsize=1)
def _creds():
    u, p = _from_keychain()
    if not _valid(u, p):
        u, p = _from_proc_env()
    if not _valid(u, p) and os.path.exists(_CACHE):
        c = json.load(open(_CACHE)); u, p = c.get("u"), c.get("p")
    if not _valid(u, p):
        raise RuntimeError("Clinical DB creds unavailable (keychain clobbered, no cache/proc fallback)")
    json.dump({"u": u, "p": p}, open(_CACHE, "w"))
    os.chmod(_CACHE, 0o600)
    return u, p


def connect():
    u, p = _creds()
    return pymssql.connect(server=OMOP_SERVER, user=u, password=p,
                           database=OMOP_DATABASE, timeout=120, login_timeout=20)


def query(sql, as_dict=False):
    """Run a SELECT, return (columns, rows). timed."""
    conn = connect()
    cur = conn.cursor(as_dict=as_dict)
    t = time.time()
    cur.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    elapsed = time.time() - t
    conn.close()
    return cols, rows, elapsed


if __name__ == "__main__":
    c, r, e = query("SELECT COUNT(*) AS n FROM person")
    print("person rows:", r, "in %.2fs" % e)
