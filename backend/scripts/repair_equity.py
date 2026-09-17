# -*- coding: utf-8 -*-
"""Back up, then narrowly repair historical equity; never fetch or reimport.

    python backend/scripts/repair_equity.py --prepare _tmp/equity-backup
    python backend/scripts/repair_equity.py --apply _tmp/equity-backup

Do not run collectors or manual imports during either phase. The fetch lock,
active-writer checks and locked before-image comparisons protect targeted rows;
read-only API requests and idle connections need not be stopped. PROCESS
visibility is required; inability to check fails closed. No lock is forced.

An interrupted/ambiguous apply leaves applying.json and refuses automatic retry.
After a commit error, reconcile DB/cache state manually: a lost commit response
is NOT evidence that the transaction rolled back. Backups are never deleted.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
# app.db currently imports app.config absolutely. Also support direct invocation.
for _path in (ROOT, BACKEND):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from backend.collector.scripts import equity as equity_helper  # noqa: E402
from backend.collector.scripts.equity import (  # noqa: E402
    PARENT_KEYS, TOTAL_KEYS, equity_close, equity_number, hk_equity_patch,
)

DATA = BACKEND / "collector" / "data"
COMPANIES = DATA / "companies"
INDEX = DATA / "index.json"
LOCK = DATA / ".fetch.lock"
SCHEMA = "equity-repair-v1"
ARTIFACTS = {"companies.zip", "index.json", "score_daily.jsonl.gz"}
CENT = Decimal("0.01")
MAX_EQUITY = Decimal("999999999999999999.99")


class RepairError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RepairError(message)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise RepairError(f"Non-finite JSON number: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    require(path.is_file() and not path.is_symlink(), f"Not a regular file: {path}")
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(row):
    """Column-aware readers supply typed DB scalars; JSON extras stay JSON."""
    return {key: str(value) if isinstance(value, Decimal) else
            value.isoformat() if isinstance(value, (date, datetime)) else value
            for key, value in dict(row).items()}


def cents(value):
    with localcontext() as ctx:
        ctx.prec = 40
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    if not result.is_finite() or abs(result) > MAX_EQUITY:
        raise ValueError("outside Numeric(20,2)")
    return result


def canonical_patch(row, market):
    if market != "HK":
        return {}, "unchanged"
    if not isinstance(row, dict):
        return {}, "incomplete_extras"
    patch, reason = hk_equity_patch(row, legacy=True)
    require(set(patch) <= {PARENT_KEYS[0], TOTAL_KEYS[0]},
            "Equity helper attempted to change a noncanonical field")
    require(not patch or reason == "repaired", "Unexpected helper result")
    return patch, reason


def alias_candidate(extras, keys):
    """No parent/total fallback; invalid or disagreeing explicit aliases veto."""
    values = []
    for key in keys:
        raw = extras.get(key)
        if raw is None:
            continue
        value = equity_number(raw)
        if value is None:
            return None, "conflict_invalid_alias"
        values.append(value)
    if not values:
        return None, "incomplete_aliases"
    if any(not equity_close(values[0], value) for value in values[1:]):
        return None, "conflict_alias"
    try:
        return cents(values[0]), None
    except (ValueError, InvalidOperation):
        return None, "conflict_numeric_range"


def plan_balance(before, market):
    """Pure plan for ONE DB row, independent of any company cache.

    Returns actual column changes (Decimal cores) and all unresolved reasons.
    A confirmed canonical repair permits replacing only its corresponding core.
    Any unresolved conflict preserves the whole row.
    """
    changes, issues = {}, []
    extras = before.get("extras")
    patch, reason = canonical_patch(extras, market)
    if reason not in ("unchanged", "repaired"):
        issues.append({"field": "extras", "reason": reason})
    if reason.startswith("conflict"):
        return {}, issues
    if not isinstance(extras, dict):
        if market != "HK":
            issues.append({"field": "extras", "reason": "incomplete_extras"})
        return changes, issues
    new_extras = dict(extras, **patch)
    if patch:
        changes["extras"] = new_extras
    for field, keys in (("equity_parent", PARENT_KEYS), ("equity_total", TOTAL_KEYS)):
        candidate, problem = alias_candidate(new_extras, keys)
        if problem:
            issues.append({"field": field, "reason": problem})
            continue
        old = before.get(field)
        if old is not None and cents(old) == candidate:
            continue
        if old is not None and keys[0] not in patch:
            issues.append({"field": field, "reason": "conflict_core",
                           "existing": str(old), "candidate": str(candidate)})
            continue
        changes[field] = candidate
    if any(issue['reason'].startswith('conflict') for issue in issues):
        return {}, issues
    return changes, issues


def plan_company(document):
    """Pure cache plan. Identity and market come ONLY from embedded fields."""
    require(isinstance(document, dict), "Company JSON must be an object")
    after = copy.deepcopy(document)
    changes, issues = [], []
    market, code = document.get("market"), document.get("code")
    if market not in ("A", "HK", "US") or not isinstance(code, str) or not code:
        return after, changes, [{"reason": "incomplete_identity"}]
    if market != "HK":
        return after, changes, issues
    rows = document.get("balance")
    if not isinstance(rows, list) or not rows:
        return after, changes, [{"reason": "incomplete_balance"}]
    for index, row in enumerate(rows):
        identity = {"row_index": index,
                    "report_date": row.get("报告日", row.get("报告期"))
                    if isinstance(row, dict) else None}
        patch, reason = canonical_patch(row, market)
        if patch:
            after["balance"][index].update(patch)
            changes.append({**identity, "before": {key: row.get(key) for key in patch},
                            "after": patch})
        elif reason != "unchanged":
            issues.append({**identity, "reason": reason})
    return after, changes, issues


def metadata(info):
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "atime_ns": info.st_atime_ns, "mode": stat.S_IMODE(info.st_mode)}


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def stable_read(path):
    first = path.lstat()
    require(stat.S_ISREG(first.st_mode), f"Not a regular, nonsymlink file: {path}")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        raw = handle.read()
        end = os.fstat(handle.fileno())
    require(stamp(first) == stamp(opened) == stamp(end) == stamp(path.lstat())
            and len(raw) == first.st_size, f"Source changed while reading: {path}")
    return raw, first


def write_new(path, raw):
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def fetch_lock():
    """Exclusive numeric PID lock, including rejection of stale/existing locks."""
    pid = str(os.getpid()).encode("ascii")
    fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    owned = os.fstat(fd)

    def is_ours():
        try:
            current = LOCK.lstat()
            return (stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino)
                    and LOCK.read_bytes() == pid)
        except OSError:
            return False

    def check():
        require(is_ours(), "Repair lost ownership of .fetch.lock; aborting")
        # Existing collectors consider an old lock stale, even for a live PID.
        os.utime(LOCK, None)

    try:
        os.write(fd, pid)
        os.fsync(fd)
        yield check
    finally:
        os.close(fd)
        if is_ours():
            LOCK.unlink()


def code_version():
    result = {"repair_sha256": file_digest(Path(__file__).resolve()),
              "helper_sha256": file_digest(Path(equity_helper.__file__).resolve())}
    try:
        result["git_head"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        result["git_head"] = None
    return result


def backup_location(argument, prepare):
    path = Path(argument).expanduser().absolute()
    require(not path.is_symlink(), "Backup directory cannot be a symlink")
    require(path.parent.is_dir(), "Backup parent must already exist")
    path = path.resolve()
    require(DATA.resolve() not in (path, *path.parents),
            "Keep repair backups outside the collector data directory")
    require(not str(path).startswith(("\\\\", "//")), "Backup must be local")
    if ROOT in path.parents:
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", str(path / "manifest.json")],
            cwd=ROOT, check=False)
        require(ignored.returncode == 0, "Choose an already git-ignored backup directory")
    if prepare:
        path.mkdir()  # Deliberately no exist_ok and no parents=True.
    else:
        require(path.is_dir(), "Backup directory does not exist")
    return path


def load_database():
    # Lazy: importing this script or testing its pure plans never opens a DB.
    from sqlalchemy import select, text, update
    from backend.app.db import SessionLocal
    from backend.app.models import EtlJobLog, FinBalance, ScoreDaily, Security
    return SessionLocal, select, text, update, EtlJobLog, FinBalance, ScoreDaily, Security


class DatabaseGuard:
    """Fresh autocommit observations, not the repair transaction's old snapshot."""
    def __init__(self, db, text, jobs, select, own_id):
        self.text, self.jobs, self.select = text, jobs, select
        self.own_id = own_id
        self.connection = db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT")
        self.guard_id = self.connection.execute(text("SELECT CONNECTION_ID()")).scalar_one()
        self.database = self.connection.execute(text("SELECT DATABASE()")).scalar_one()

    def close(self):
        self.connection.close()

    def check(self):
        t, connection = self.text, self.connection
        running = connection.execute(self.select(self.jobs.id, self.jobs.job_name).where(
            self.jobs.status == "running")).all()
        require(not running, f"etl_job_log has running jobs (including stale entries): {running}")
        transactions = connection.execute(t(
            "SELECT trx_mysql_thread_id FROM information_schema.innodb_trx "
            "WHERE trx_rows_modified > 0")).scalars().all()
        clients = connection.execute(t("SHOW FULL PROCESSLIST")).mappings().all()
        busy = []
        for row in clients:
            if row["Id"] in (self.own_id, self.guard_id) or row["db"] != self.database:
                continue
            sql = (row["Info"] or "").lstrip().upper()
            read_only = row["Command"] == "Sleep" or sql.startswith(
                ("SELECT ", "SHOW ", "EXPLAIN ", "DESCRIBE "))
            if row["Id"] in transactions or not read_only:
                busy.append({"id": row["Id"], "command": row["Command"]})
        require(not busy, f"Concurrent target-DB writers/importers: {busy}")


@contextmanager
def database_phase(prepare):
    api = load_database()
    SessionLocal, select, text, _, jobs, balance, scores, security = api
    with SessionLocal() as db:
        require(db.get_bind().dialect.name == "mysql", "Only MySQL/InnoDB is supported")
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        if prepare:
            db.execute(text("SET TRANSACTION READ ONLY"))
        own_id = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
        tables = db.execute(text(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN "
            "('fin_balance', 'security', 'score_daily', 'etl_job_log')")).all()
        require(len(tables) == 4 and all(engine == "InnoDB" for _, engine in tables),
                "All four required tables must exist and use InnoDB")
        guard = DatabaseGuard(db, text, jobs, select, own_id)
        try:
            guard.check()
            if not prepare:
                # A full locking index scan also blocks new collector.run log inserts
                # until commit under InnoDB REPEATABLE READ (including the empty case).
                db.execute(select(jobs.id).order_by(jobs.id).with_for_update()).all()
                guard.check()
            url = db.get_bind().url
            fingerprint = digest(json_bytes([url.drivername, url.host, url.port, url.database]))
            yield db, guard, fingerprint, api
        finally:
            guard.close()
            # Explicit rollback also ends prepare's read-only snapshot.
            db.rollback()


def add_issues(manifest, identity, issues):
    for issue in issues:
        bucket = "conflicts" if issue["reason"].startswith("conflict") else "incomplete"
        manifest[bucket].append({**identity, **issue})


def assert_live(path, expected_hash, expected_meta):
    raw, info = stable_read(path)
    require(digest(raw) == expected_hash and info.st_mtime_ns == expected_meta["mtime_ns"]
            and stat.S_IMODE(info.st_mode) == expected_meta["mode"],
            f"Source no longer matches prepared backup: {path}")
    return info


def prepare_backup(backup, check_lock):
    version = code_version()
    manifest = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
                "code_version": version, "artifacts": {}, "files": [], "db_rows": [],
                "conflicts": [], "incomplete": [], "summary": {}}
    sources = {}
    paths = sorted(COMPANIES.glob("*.json"))
    require(COMPANIES.is_dir() and not COMPANIES.is_symlink(), "Invalid companies directory")
    raw, info = stable_read(INDEX)
    write_new(backup / "index.json", raw)
    manifest["index_source"] = {"sha256": digest(raw), **metadata(info)}
    sources[INDEX] = (digest(raw), stamp(info))
    with database_phase(True) as (db, guard, fingerprint, api):
        _, select, _, _, _, balance, scores, security = api
        manifest["database_fingerprint"] = fingerprint
        with zipfile.ZipFile(backup / "companies.zip", "x", zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                check_lock()
                raw, info = stable_read(path)
                sources[path] = (digest(raw), stamp(info))
                document = parse_json(raw)
                after, changes, issues = plan_company(document)
                identity = {"source": "cache", "file": path.name,
                            "code": document.get("code"), "market": document.get("market")}
                add_issues(manifest, identity, issues)
                if changes:
                    post = json_bytes(after)
                    archive.writestr("before/" + path.name, raw)
                    archive.writestr("after/" + path.name, post)
                    manifest["files"].append({"name": path.name, "code": document["code"],
                                              "market": document["market"],
                                              "before_sha256": digest(raw), "after_sha256": digest(post),
                                              "metadata": metadata(info), "rows": changes})
        guard.check()
        securities = {row.sid: {"code": row.code, "market": row.market} for row in db.execute(
            select(security.sid, security.code, security.market))}
        count = 0
        result = db.execute(select(balance.__table__).order_by(balance.sid, balance.report_date)
                            .execution_options(yield_per=500))
        for row in result.mappings():
            count += 1
            if count % 500 == 1:
                check_lock()
            identity = {"source": "db", "sid": row["sid"],
                        "report_date": row["report_date"].isoformat()}
            sec = securities.get(row["sid"])
            if sec is None:
                add_issues(manifest, identity, [{"reason": "incomplete_security"}])
                continue
            identity.update(sec)
            before = snapshot(row)
            changes, issues = plan_balance(before, sec["market"])
            add_issues(manifest, identity, issues)
            if changes:
                manifest["db_rows"].append({"sid": row["sid"], "report_date": identity["report_date"],
                                            **sec, "before": before,
                                            "after": {**before, **snapshot(changes)}})
        result.close()
        guard.check()
        score_count = 0
        # All dates, all sids: later --only-scores may touch any of them.
        with (backup / "score_daily.jsonl.gz").open("xb") as stream:
            with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
                result = db.execute(select(scores.__table__).order_by(scores.sid, scores.trade_date)
                                    .execution_options(yield_per=500))
                for row in result.mappings():
                    compressed.write(json_bytes(snapshot(row)))
                    score_count += 1
                    if score_count % 500 == 1:
                        check_lock()
                result.close()
            stream.flush()
            os.fsync(stream.fileno())
        guard.check()
        require(paths == sorted(COMPANIES.glob("*.json")), "Company file inventory changed during prepare")
        # Check every source, including files that needed no changes and index.json.
        for path, (expected_hash, expected_stamp) in sources.items():
            check_lock()
            current, current_info = stable_read(path)
            require(digest(current) == expected_hash and stamp(current_info) == expected_stamp,
                    f"Source changed during prepare: {path}")
        guard.check()
        require(code_version() == version, "Repair/helper code changed during prepare")
    manifest["summary"] = {
        "company_files_scanned": len(paths), "cache_files_changed": len(manifest["files"]),
        "cache_rows_changed": sum(len(item["rows"]) for item in manifest["files"]),
        "db_rows_scanned": count, "db_rows_changed": len(manifest["db_rows"]),
        "db_extras_changed": sum(item["before"]["extras"] != item["after"]["extras"]
                                 for item in manifest["db_rows"]),
        "core_values_changed": sum(item["before"][key] != item["after"][key]
                                   for item in manifest["db_rows"]
                                   for key in ("equity_parent", "equity_total")),
        "score_rows_backed_up": score_count, "conflicts": len(manifest["conflicts"]),
        "incomplete": len(manifest["incomplete"]),
    }
    for name in sorted(ARTIFACTS):
        path = backup / name
        # Windows FlushFileBuffers needs a writable handle; these are backups,
        # never source files. Flush zip output before making the manifest usable.
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
        manifest["artifacts"][name] = {"sha256": file_digest(path), "size": path.stat().st_size}
    check_lock()
    raw = json_bytes(manifest)
    write_new(backup / "manifest.json", raw)
    write_new(backup / "manifest.sha256", (digest(raw) + "\n").encode("ascii"))
    return manifest["summary"]


def validate_manifest(backup, check_lock):
    require(not (backup / "completed.json").exists(), "Already completed; do not replay this backup")
    require(not (backup / "applying.json").exists(),
            "Interrupted/ambiguous apply detected; manual reconciliation required")
    raw, _ = stable_read(backup / "manifest.json")
    checksum, _ = stable_read(backup / "manifest.sha256")
    require(checksum.decode("ascii").strip() == digest(raw), "Manifest checksum mismatch")
    manifest = parse_json(raw)
    required = {"schema", "created_at", "code_version", "artifacts", "files", "db_rows",
                "conflicts", "incomplete", "summary", "index_source", "database_fingerprint"}
    require(isinstance(manifest, dict) and set(manifest) == required
            and manifest["schema"] == SCHEMA, "Unknown/invalid manifest safety schema")
    for key in ("files", "db_rows", "conflicts", "incomplete"):
        require(isinstance(manifest[key], list), f"Invalid manifest list: {key}")
    current_version = code_version()
    require(all(manifest["code_version"][key] == current_version[key]
                for key in ("repair_sha256", "helper_sha256")), "Repair/helper code has changed; prepare again")
    require(set(manifest["artifacts"]) == ARTIFACTS, "Missing/unexpected backup artifacts")
    for name, expected in manifest["artifacts"].items():
        check_lock()
        path = backup / name
        require(set(expected) == {"sha256", "size"} and file_digest(path) == expected["sha256"]
                and path.stat().st_size == expected["size"], f"Backup artifact mismatch: {name}")
    require(manifest["index_source"]["sha256"] == manifest["artifacts"]["index.json"]["sha256"],
            "Index backup hash mismatch")
    names = set()
    with zipfile.ZipFile(backup / "companies.zip") as archive:
        for item in manifest["files"]:
            check_lock()
            require(set(item) == {"name", "code", "market", "before_sha256", "after_sha256", "metadata", "rows"},
                    "Invalid cache entry schema")
            name = item["name"]
            require(isinstance(name, str) and name.endswith(".json") and name not in names
                    and not any(char in name for char in ("/", "\\", ":"))
                    and Path(name).name == name, "Unsafe/duplicate company filename")
            names.add(name)
            meta = item["metadata"]
            require(set(meta) == {"size", "mtime_ns", "atime_ns", "mode"}
                    and all(type(value) is int and value >= 0 for value in meta.values())
                    and meta["mode"] <= 0o7777, "Invalid file metadata")
            before = archive.read("before/" + name)
            after = archive.read("after/" + name)
            require(digest(before) == item["before_sha256"] and len(before) == meta["size"]
                    and digest(after) == item["after_sha256"], f"Cache backup mismatch: {name}")
            document = parse_json(before)
            planned, changes, _ = plan_company(document)
            require(document.get("code") == item["code"] and document.get("market") == item["market"] == "HK"
                    and changes and changes == item["rows"] and json_bytes(planned) == after,
                    f"Unsafe cache patch: {name}")
        expected_members = {prefix + name for name in names for prefix in ("before/", "after/")}
        require(len(archive.namelist()) == len(expected_members)
                and set(archive.namelist()) == expected_members, "Unexpected/duplicate zip entries")
    api = load_database()
    _, _, _, _, _, balance, scores, _ = api
    columns = set(balance.__table__.columns.keys())
    seen = set()
    for item in manifest["db_rows"]:
        require(set(item) == {"sid", "report_date", "code", "market", "before", "after"},
                "Invalid DB entry schema")
        key = (item["sid"], item["report_date"])
        require(type(item["sid"]) is int and item["sid"] > 0 and key not in seen
                and date.fromisoformat(item["report_date"]).isoformat() == item["report_date"]
                and item["market"] in ("A", "HK", "US")
                and isinstance(item["code"], str) and item["code"], "Invalid/duplicate DB identity")
        seen.add(key)
        before, after = item["before"], item["after"]
        require(set(before) == columns == set(after)
                and before["sid"] == item["sid"] and before["report_date"] == item["report_date"],
                "Incomplete/mismatched DB row backup")
        changes, _ = plan_balance(before, item["market"])
        require(changes and json_bytes({**before, **snapshot(changes)}) == json_bytes(after),
                f"Unsafe DB patch: {key}")
    count = 0
    with gzip.open(backup / "score_daily.jsonl.gz", "rb") as handle:
        for line in handle:
            require(set(parse_json(line)) == set(scores.__table__.columns.keys()), "Invalid score row backup")
            count += 1
            if count % 500 == 1:
                check_lock()
    require(count == manifest["summary"]["score_rows_backed_up"], "Score backup count mismatch")
    require(len(names) == manifest["summary"]["cache_files_changed"]
            and len(seen) == manifest["summary"]["db_rows_changed"], "Manifest count mismatch")
    return manifest, digest(raw)


def stage_file(path, raw, meta):
    """Stage on the same filesystem, preserving financial freshness and mode."""
    fd, temporary = tempfile.mkstemp(prefix=".equity-repair-", suffix=".tmp", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.chmod(temp, meta["mode"])
            os.utime(temp, ns=(meta["atime_ns"], meta["mtime_ns"]))
            os.fsync(handle.fileno())
        return temp
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def restore_changed(archive, attempts):
    """Never restore a file that has since been replaced/edited by someone else."""
    failures = []
    for item, installed_inode in reversed(attempts):
        path = COMPANIES / item["name"]
        temp = None
        try:
            raw, info = stable_read(path)
            if digest(raw) == item["before_sha256"]:
                continue  # os.replace did not happen, or original already restored.
            require((info.st_dev, info.st_ino) == installed_inode
                    and digest(raw) == item["after_sha256"]
                    and info.st_mtime_ns == item["metadata"]["mtime_ns"],
                    f"External change; NOT restoring {path}")
            temp = stage_file(path, archive.read("before/" + item["name"]), item["metadata"])
            # Check again after staging, immediately before replacement.
            current, current_info = stable_read(path)
            require(stamp(info) == stamp(current_info) and current == raw,
                    f"External change; NOT restoring {path}")
            os.replace(temp, path)
        except BaseException as error:
            failures.append(f"{path}: {error}")
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    return failures


def apply_backup(backup, check_lock):
    manifest, manifest_hash = validate_manifest(backup, check_lock)
    attempts, staged = [], []
    started = False
    commit_started = False
    with zipfile.ZipFile(backup / "companies.zip") as archive, database_phase(False) as phase:
        db, guard, fingerprint, api = phase
        _, select, _, update, _, balance, _, security = api
        require(fingerprint == manifest["database_fingerprint"], "Wrong target database")
        try:
            securities = {}
            for item in sorted(manifest["db_rows"], key=lambda row: (row["sid"], row["report_date"])):
                check_lock()
                sid = item["sid"]
                if sid not in securities:
                    sec = db.execute(select(security.code, security.market).where(
                        security.sid == sid).with_for_update()).mappings().one_or_none()
                    require(sec is not None, f"Security disappeared: {sid}")
                    securities[sid] = dict(sec)
                require(securities[sid] == {"code": item["code"], "market": item["market"]},
                        f"Security identity changed: {sid}")
                row = db.execute(select(balance.__table__).where(
                    balance.sid == sid, balance.report_date == date.fromisoformat(item["report_date"]))
                    .with_for_update()).mappings().one_or_none()
                require(row is not None and json_bytes(snapshot(row)) == json_bytes(item["before"]),
                        f"DB row changed since prepare: {sid}/{item['report_date']}")
            # ALL rows locked and compared, ALL backups verified, ALL live files
            # compared before the first UPDATE or replacement of a real cache.
            assert_live(INDEX, manifest["index_source"]["sha256"], manifest["index_source"])
            for item in manifest["files"]:
                check_lock()
                assert_live(COMPANIES / item["name"], item["before_sha256"], item["metadata"])
            guard.check()
            check_lock()
            write_new(backup / "applying.json", json_bytes({"manifest_sha256": manifest_hash,
                      "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}))
            started = True
            for item in manifest["files"]:
                check_lock()
                temp = stage_file(COMPANIES / item["name"], archive.read("after/" + item["name"]), item["metadata"])
                staged.append((item, temp))
            guard.check()
            for item in manifest["db_rows"]:
                check_lock()
                changes, _ = plan_balance(item["before"], item["market"])
                predicate = (balance.sid == item["sid"],
                             balance.report_date == date.fromisoformat(item["report_date"]))
                result = db.execute(update(balance.__table__).where(*predicate).values(
                    **changes, updated_at=balance.updated_at))
                require(result.rowcount == 1, f"Unexpected update count for {item['sid']}/{item['report_date']}")
                actual = db.execute(select(balance.__table__).where(*predicate)).mappings().one()
                require(json_bytes(snapshot(actual)) == json_bytes(item["after"]),
                        "DB postimage mismatch (including updated_at); rolling back")
            for item, temp in staged:
                check_lock()
                path = COMPANIES / item["name"]
                assert_live(path, item["before_sha256"], item["metadata"])
                info = temp.stat()
                attempts.append((item, (info.st_dev, info.st_ino)))
                os.replace(temp, path)
            # Last guard before commit. No index, quote or score writes, ever.
            check_lock()
            assert_live(INDEX, manifest["index_source"]["sha256"], manifest["index_source"])
            for item in manifest["files"]:
                assert_live(COMPANIES / item["name"], item["after_sha256"], item["metadata"])
            guard.check()
            commit_started = True
            db.commit()
            write_new(backup / "completed.json", json_bytes({"manifest_sha256": manifest_hash,
                      "committed_at": datetime.now(timezone.utc).isoformat(), "summary": manifest["summary"]}))
        except BaseException as error:
            if commit_started:
                raise RepairError("Commit attempted: do NOT restore or replay automatically. "
                                  f"Reconcile using {backup}; original error: {error}") from error
            rollback_error = None
            try:
                db.rollback()
            except BaseException as failure:
                rollback_error = failure
            failures = restore_changed(archive, attempts)
            if started and rollback_error is None and not failures:
                (backup / "applying.json").unlink()
            if rollback_error or failures:
                raise RepairError(f"Manual reconciliation required in {backup}; "
                                  f"rollback={rollback_error}; restore={failures}") from error
            raise
        finally:
            for _, temp in staged:
                temp.unlink(missing_ok=True)
    return manifest["summary"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--prepare", metavar="BACKUP_DIR", help="Read-only plan and NEW backup directory")
    phase.add_argument("--apply", metavar="BACKUP_DIR", help="Validate and apply an existing prepared backup")
    args = parser.parse_args(argv)
    try:
        with fetch_lock() as check_lock:
            backup = backup_location(args.prepare or args.apply, bool(args.prepare))
            summary = prepare_backup(backup, check_lock) if args.prepare else apply_backup(backup, check_lock)
        print(json.dumps({"phase": "prepare" if args.prepare else "apply", "backup": str(backup),
                          **summary}, ensure_ascii=False))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"Equity repair aborted: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
