from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .model import GameFilter, SearchSettings, Source, Variation, encode

SCHEMA = """
CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE engines (id TEXT PRIMARY KEY, profile TEXT NOT NULL, installed TEXT NOT NULL);
CREATE TABLE scans (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'running',
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, filters TEXT NOT NULL, rules TEXT NOT NULL,
    profile TEXT NOT NULL, game_limit INTEGER NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0, analyzed INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0,
    predicted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scans,
    spec TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0, malformed INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending', error TEXT
);
CREATE INDEX sources_scan ON sources(scan_id, id);
CREATE TABLE games (
    id TEXT PRIMARY KEY, white TEXT, black TEXT, white_elo INTEGER, black_elo INTEGER,
    result TEXT, day TEXT, speed TEXT, time_control TEXT, opening TEXT, eco TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE scan_games (
    id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scans,
    game_id TEXT NOT NULL REFERENCES games,
    reviewed INTEGER NOT NULL DEFAULT 0, bookmarked INTEGER NOT NULL DEFAULT 0,
    cursor INTEGER NOT NULL DEFAULT 0, cursor_moment INTEGER, UNIQUE (scan_id, game_id)
);
CREATE INDEX scan_games_game ON scan_games(game_id, id);
CREATE TABLE game_sources (
    game_id TEXT NOT NULL REFERENCES games, source_id INTEGER NOT NULL REFERENCES sources,
    PRIMARY KEY (game_id, source_id)
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scans,
    stage TEXT NOT NULL, target INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, UNIQUE (stage, target)
);
CREATE INDEX jobs_pick ON jobs(stage, state, id);
CREATE INDEX jobs_scan ON jobs(scan_id, state, stage);
CREATE INDEX jobs_unfinished ON jobs(scan_id, state, stage) WHERE state!='done';
CREATE TABLE evaluations (
    scan_game INTEGER NOT NULL REFERENCES scan_games,
    ply INTEGER NOT NULL, score TEXT NOT NULL, depth INTEGER NOT NULL,
    PRIMARY KEY (scan_game, ply)
);
CREATE TABLE searches (id TEXT PRIMARY KEY, lines TEXT NOT NULL);
CREATE TABLE policies (id TEXT PRIMARY KEY, moves TEXT NOT NULL);
CREATE TABLE mistakes (
    id INTEGER PRIMARY KEY, scan_game INTEGER NOT NULL REFERENCES scan_games,
    ply INTEGER NOT NULL, position TEXT NOT NULL, played TEXT NOT NULL,
    detected TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    discovery TEXT NOT NULL, detail TEXT, state TEXT NOT NULL DEFAULT 'candidate',
    loss_cp INTEGER, clock REAL, probability REAL, before_policy TEXT REFERENCES policies,
    after_policy TEXT REFERENCES policies, maia_elo INTEGER,
    reviewed INTEGER NOT NULL DEFAULT 0, bookmarked INTEGER NOT NULL DEFAULT 0,
    UNIQUE (scan_game, ply)
);
CREATE INDEX mistakes_probability ON mistakes(state, probability DESC, loss_cp DESC, id DESC);
CREATE INDEX verified_rank ON mistakes(probability DESC, (loss_cp IS NULL) DESC, loss_cp DESC, id DESC)
    WHERE state IN ('verified','awaiting_maia','complete');
CREATE INDEX mistakes_position ON mistakes(position);
CREATE TABLE occurrences (
    game_id TEXT NOT NULL REFERENCES games, ply INTEGER NOT NULL,
    position TEXT NOT NULL, played TEXT NOT NULL, PRIMARY KEY (game_id, ply)
);
CREATE INDEX occurrences_position ON occurrences(position, played, game_id);
CREATE TABLE position_frequency (
    position TEXT NOT NULL, played TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (position, played)
);
CREATE TRIGGER occurrence_count AFTER INSERT ON occurrences BEGIN
    INSERT INTO position_frequency VALUES (NEW.position, NEW.played, 1)
    ON CONFLICT (position, played) DO UPDATE SET count=count+1;
END;
PRAGMA user_version=3;
"""


def _decoded(row, *names) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for name in names:
        if item[name] is not None:
            item[name] = json.loads(item[name])
    return item


class Library:
    """Thread-local readers and short serial write transactions in WAL mode."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.local = threading.local()
        self.writer = threading.RLock()
        self.runner_lock = None
        db = self.connection()
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        elif version in (1, 2):
            if version == 1:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE scan_games ADD COLUMN cursor_moment INTEGER;
                    CREATE INDEX jobs_unfinished ON jobs(scan_id, state, stage) WHERE state!='done';
                    PRAGMA user_version=2;
                    COMMIT;
                """)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE INDEX verified_rank ON mistakes(probability DESC, (loss_cp IS NULL) DESC, loss_cp DESC, id DESC)
                    WHERE state IN ('verified','awaiting_maia','complete');
                PRAGMA user_version=3;
                COMMIT;
            """)
        elif version != 3:
            raise RuntimeError(f"This application cannot open database schema version {version}.")

    def connection(self) -> sqlite3.Connection:
        db = getattr(self.local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=NORMAL")
            self.local.db = db
        return db

    def close_thread(self) -> None:
        db = getattr(self.local, "db", None)
        if db is not None:
            db.close()
            del self.local.db

    @contextmanager
    def transaction(self):
        with self.writer:
            db = self.connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def setting(self, name: str, default=None):
        row = self.connection().execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, name: str, value) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       (name, encode(value)))

    def engines(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.connection().execute(
            "SELECT profile FROM engines ORDER BY installed DESC, id",
        )]

    def engine(self, identity: str) -> dict | None:
        row = self.connection().execute("SELECT profile FROM engines WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_engine(self, profile: dict) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO engines VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET profile=excluded.profile",
                       (profile["id"], encode(profile), profile["installed"]))

    def engine_needed(self, identity: str) -> bool:
        return self.connection().execute("""
            SELECT 1 FROM scans WHERE json_extract(profile, '$.id')=?
            AND (((game_limit=0 OR selected<game_limit) AND EXISTS (
                     SELECT 1 FROM sources WHERE scan_id=scans.id AND state IN ('pending','running','failed')))
                 OR EXISTS (SELECT 1 FROM jobs WHERE scan_id=scans.id AND stage!='maia' AND state!='done'))
            LIMIT 1
        """, (identity,)).fetchone() is not None

    def delete_engine(self, identity: str) -> None:
        with self.transaction() as db:
            if self.engine_needed(identity):
                raise ValueError("An unfinished scan still needs this engine.")
            db.execute("DELETE FROM settings WHERE name='active_engine' AND value=?", (encode(identity),))
            db.execute("DELETE FROM engines WHERE id=?", (identity,))

    def create_scan(self, name: str, sources: list[Source], filters: GameFilter,
                    rules: SearchSettings, profile: dict, limit: int) -> int:
        if limit < 0 or not sources:
            raise ValueError("Choose at least one source and a valid game limit.")
        with self.transaction() as db:
            scan_id = db.execute("""
                INSERT INTO scans(name, filters, rules, profile, game_limit) VALUES (?, ?, ?, ?, ?)
            """, (name, encode(asdict(filters)), encode(asdict(rules)), encode(profile), limit)).lastrowid
            db.executemany("INSERT INTO sources(scan_id, spec) VALUES (?, ?)",
                           [(scan_id, encode(asdict(source))) for source in sources])
        return scan_id

    def recover(self) -> None:
        with self.transaction() as db:
            db.execute("UPDATE sources SET state='pending' WHERE state='running'")
            db.execute("UPDATE jobs SET state='pending' WHERE state='running'")
            db.execute("UPDATE scans SET state='paused' WHERE state='running'")

    def scans(self, limit: int = 100) -> list[dict]:
        return [_decoded(row, "filters", "rules", "profile") for row in self.connection().execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,),
        )]

    def scan(self, identity: int) -> dict | None:
        return _decoded(self.connection().execute("SELECT * FROM scans WHERE id=?", (identity,)).fetchone(),
                        "filters", "rules", "profile")

    def set_state(self, scan_id: int, state: str) -> None:
        if state not in ("running", "paused", "canceled"):
            raise ValueError("Unknown scan state.")
        with self.transaction() as db:
            db.execute("UPDATE scans SET state=? WHERE id=?", (state, scan_id))
            if state == "running":
                db.execute("UPDATE sources SET state='pending', error=NULL WHERE scan_id=? AND state='failed'", (scan_id,))
                db.execute("UPDATE jobs SET state='pending', error=NULL WHERE scan_id=? AND state='failed'", (scan_id,))

    def ready_source(self) -> dict | None:
        with self.transaction() as db:
            item = db.execute("""
                SELECT sources.* FROM sources JOIN scans ON scans.id=sources.scan_id
                WHERE sources.state='pending' AND scans.state='running'
                AND (scans.game_limit=0 OR scans.selected<scans.game_limit) ORDER BY sources.id LIMIT 1
            """).fetchone()
            if item is not None:
                db.execute("UPDATE sources SET state='running', error=NULL WHERE id=?", (item["id"],))
            return _decoded(item, "spec")

    def source_state(self, identity: int, state: str, error: str | None = None) -> None:
        with self.transaction() as db:
            db.execute("UPDATE sources SET state=?, error=? WHERE id=?", (state, error, identity))

    def checkpoint(self, identity: int, index: int, skipped: int, malformed: int) -> None:
        with self.transaction() as db:
            db.execute("""
                UPDATE sources SET seen=MAX(seen, ?), skipped=skipped+?, malformed=malformed+? WHERE id=?
            """, (index, skipped, malformed, identity))

    def import_game(self, source_id: int, scan_id: int, game: dict,
                    index: int, skipped: int = 0, malformed: int = 0) -> bool:
        with self.transaction() as db:
            db.execute("""
                UPDATE sources SET seen=MAX(seen, ?), skipped=skipped+?, malformed=malformed+? WHERE id=?
            """, (index, skipped, malformed, source_id))
            db.execute("""
                INSERT OR IGNORE INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (*[game[n] for n in ("id", "white", "black", "white_elo", "black_elo",
                                     "result", "day", "speed", "time_control", "opening", "eco")], encode(game)))
            db.execute("INSERT OR IGNORE INTO game_sources VALUES (?, ?)", (game["id"], source_id))
            result = db.execute("INSERT OR IGNORE INTO scan_games(scan_id, game_id) VALUES (?, ?)", (scan_id, game["id"]))
            if result.rowcount == 0:
                return False
            db.execute("UPDATE scans SET selected=selected+1 WHERE id=?", (scan_id,))
            db.execute("INSERT INTO jobs(scan_id, stage, target) VALUES (?, 'discovery', ?)", (scan_id, result.lastrowid))
        return True

    def claim(self, stage: str) -> dict | None:
        with self.transaction() as db:
            job = db.execute("""
                SELECT jobs.* FROM jobs JOIN scans ON scans.id=jobs.scan_id
                WHERE jobs.stage=? AND jobs.state='pending' AND scans.state='running'
                AND (jobs.stage='maia' OR (
                    SELECT COUNT(*) FROM jobs AS active
                    WHERE active.scan_id=jobs.scan_id AND active.state='running' AND active.stage!='maia'
                ) < json_extract(scans.rules, '$.workers'))
                ORDER BY jobs.id LIMIT 1
            """, (stage,)).fetchone()
            if job is None:
                return None
            db.execute("UPDATE jobs SET state='running', attempts=attempts+1, error=NULL WHERE id=?", (job["id"],))
        return dict(job)

    def fail_job(self, identity: int, error: str, *, pause_scan: bool = False) -> None:
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='failed', error=? WHERE id=?", (error, identity))
            if pause_scan:
                db.execute("""
                    UPDATE scans SET state='paused' WHERE state='running'
                    AND id=(SELECT scan_id FROM jobs WHERE id=?)
                """, (identity,))

    def release_job(self, identity: int) -> None:
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='pending', error=NULL WHERE id=?", (identity,))

    def outstanding(self, stages: tuple[str, ...], scan_id: int | None = None) -> int:
        match = ",".join("?" for _ in stages)
        sql = f"SELECT COUNT(*) FROM jobs WHERE stage IN ({match}) AND state IN ('pending','running')"
        args = list(stages)
        if scan_id is not None:
            sql += " AND scan_id=?"
            args.append(scan_id)
        return self.connection().execute(sql, args).fetchone()[0]

    def finish_discovery(self, job: dict, game: dict, traces: list, candidates: list, occurrences: list) -> None:
        with self.transaction() as db:
            target, scan_id = job["target"], job["scan_id"]
            db.executemany("INSERT INTO evaluations VALUES (?, ?, ?, ?)",
                           [(target, ply, encode(score), depth) for ply, score, depth in traces])
            db.executemany("INSERT OR IGNORE INTO occurrences VALUES (?, ?, ?, ?)",
                           [(game["id"], ply, position, played) for ply, position, played in occurrences])
            for move in candidates:
                result = db.execute("""
                    INSERT INTO mistakes(scan_game, ply, position, played, discovery, loss_cp, clock)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (target, move["ply"], move["position"], move["played"], encode(move), move["loss_cp"], move["clock"]))
                db.execute("INSERT INTO jobs(scan_id, stage, target) VALUES (?, 'detail', ?)", (scan_id, result.lastrowid))
            db.execute("UPDATE scans SET analyzed=analyzed+1, candidates=candidates+? WHERE id=?", (len(candidates), scan_id))
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))

    def cached_search(self, identity: str) -> list[Variation] | None:
        row = self.connection().execute("SELECT lines FROM searches WHERE id=?", (identity,)).fetchone()
        return [Variation.load(line) for line in json.loads(row[0])] if row else None

    def save_search(self, identity: str, lines: list[Variation]) -> list[Variation]:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO searches VALUES (?, ?)", (identity, encode([line.dump() for line in lines])))
        return self.cached_search(identity)

    def finish_detail(self, job: dict, detail: dict, elo: int | None) -> None:
        scan_id, identity = job["scan_id"], job["target"]
        verified = detail["verified"]
        state = ("awaiting_maia" if elo is not None else "verified") if verified else "rejected"
        followup = detail.pop("followup", None)
        with self.transaction() as db:
            db.execute("UPDATE mistakes SET detail=?, state=?, loss_cp=?, maia_elo=? WHERE id=?",
                       (encode(detail), state, detail["loss_cp"], elo if verified else None, identity))
            if verified:
                db.execute("UPDATE scans SET verified=verified+1 WHERE id=?", (scan_id,))
                if elo is not None:
                    db.execute("INSERT INTO jobs(scan_id, stage, target) VALUES (?, 'maia', ?)", (scan_id, identity))
            if followup:
                scan_game = db.execute("SELECT scan_game FROM mistakes WHERE id=?", (identity,)).fetchone()[0]
                result = db.execute("""
                    INSERT OR IGNORE INTO mistakes(scan_game,ply,position,played,discovery,loss_cp,clock)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (scan_game, followup["ply"], followup["position"], followup["played"],
                      encode(followup), followup["loss_cp"], followup["clock"]))
                if result.rowcount:
                    db.execute("INSERT INTO jobs(scan_id, stage, target) VALUES (?, 'detail', ?)", (scan_id, result.lastrowid))
                    db.execute("UPDATE scans SET candidates=candidates+1 WHERE id=?", (scan_id,))
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))

    def policy(self, identity: str | None) -> dict | None:
        if identity is None:
            return None
        row = self.connection().execute("SELECT moves FROM policies WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_policy(self, identity: str, moves: dict) -> dict:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO policies VALUES (?, ?)", (identity, encode(moves)))
        return self.policy(identity)

    def finish_maia(self, job: dict, before: str, after: str, probability: float) -> None:
        with self.transaction() as db:
            db.execute("""
                UPDATE mistakes SET state='complete', before_policy=?, after_policy=?, probability=? WHERE id=?
            """, (before, after, probability, job["target"]))
            db.execute("UPDATE scans SET predicted=predicted+1 WHERE id=?", (job["scan_id"],))
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))

    def game(self, scan_game: int) -> dict | None:
        row = self.connection().execute("""
            SELECT games.payload, scan_games.* FROM scan_games JOIN games ON games.id=scan_games.game_id
            WHERE scan_games.id=?
        """, (scan_game,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(json.loads(result.pop("payload")))
        result["scan_game"] = scan_game
        return result

    def evaluations(self, scan_game: int) -> dict[int, dict]:
        return {row["ply"]: {"score": json.loads(row["score"]), "depth": row["depth"]}
                for row in self.connection().execute(
                    "SELECT ply, score, depth FROM evaluations WHERE scan_game=? ORDER BY ply", (scan_game,),
                )}

    def mistake(self, identity: int) -> dict | None:
        return _decoded(self.connection().execute("SELECT * FROM mistakes WHERE id=?", (identity,)).fetchone(),
                        "discovery", "detail")

    def moments(self, scan_game: int) -> list[dict]:
        rows = self.connection().execute("""
            SELECT * FROM mistakes WHERE scan_game=? AND state!='rejected' ORDER BY ply
        """, (scan_game,))
        return [_decoded(row, "discovery", "detail") for row in rows]

    def frequency(self, position: str) -> dict[str, int]:
        return dict(self.connection().execute("SELECT played, count FROM position_frequency WHERE position=?", (position,)))

    def position_games(self, position: str, limit: int = 50) -> list[dict]:
        return [dict(row) for row in self.connection().execute("""
            SELECT occurrences.ply, occurrences.played, games.white, games.black, games.day,
                   (SELECT MAX(id) FROM scan_games WHERE game_id=games.id) AS scan_game
            FROM occurrences JOIN games ON games.id=occurrences.game_id
            WHERE occurrences.position=? LIMIT ?
        """, (position, limit))]

    def review(self, identity: int, *, game: bool = False, **values) -> None:
        allowed = {"reviewed", "bookmarked", "cursor", "cursor_moment"} if game else {"reviewed", "bookmarked"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Unknown review setting.")
        table = "scan_games" if game else "mistakes"
        columns = ", ".join(f"{name}=?" for name in values)
        with self.transaction() as db:
            db.execute(f"UPDATE {table} SET {columns} WHERE id=?", (*values.values(), identity))

    def game_page(self, query="", offset=0, limit=100, *, bookmarks=False, sort="recent", speed="", time_control="") -> list[dict]:
        order = {"recent": "scan_games.id DESC", "date": "games.day DESC", "white": "games.white COLLATE NOCASE",
                 "black": "games.black COLLATE NOCASE", "rating": "games.white_elo DESC, games.black_elo DESC",
                 "speed": "games.speed", "opening": "games.opening COLLATE NOCASE"}.get(sort, "scan_games.id DESC")
        args = []
        sql = """
            SELECT scan_games.*, games.white, games.black, games.white_elo, games.black_elo,
                   games.result, games.day, games.speed, games.time_control, games.opening, games.eco,
                   jobs.state AS analysis_state
            FROM scan_games JOIN games ON games.id=scan_games.game_id
            JOIN jobs ON jobs.stage='discovery' AND jobs.target=scan_games.id
            WHERE 1=1
        """
        if query:
            sql += " AND (LOWER(games.white) LIKE ? OR LOWER(games.black) LIKE ? OR LOWER(games.opening) LIKE ?)"
            args.extend([f"%{query.lower()}%"] * 3)
        if speed:
            sql += " AND games.speed=?"
            args.append(speed)
        if time_control:
            sql += " AND games.time_control=?"
            args.append(time_control)
        if bookmarks:
            sql += " AND scan_games.bookmarked=1"
        rows = self.connection().execute(sql + f" ORDER BY {order},scan_games.id DESC LIMIT ? OFFSET ?",
                                         [*args, limit, offset])
        games = []
        for row in rows:
            item = dict(row)
            moments = self.moments(row["id"])
            verified = {m["ply"] for m in moments if m["state"] != "candidate"}
            item["moments"] = len(verified)
            item["missed"] = sum(ply - 1 in verified for ply in verified)
            item["pending"] = sum(m["state"] in ("candidate", "awaiting_maia") for m in moments)
            games.append(item)
        return games

    def discovery_page(
        self, query="", offset=0, limit=100, *, sort="probability", pending=False,
        review="all", clock: float | None = None, clock_min: float | None = None, eco="", speed="", time_control="",
    ) -> list[dict]:
        order = {
            "probability": "mistakes.probability DESC, (mistakes.loss_cp IS NULL) DESC,mistakes.loss_cp DESC",
            "loss": "(mistakes.loss_cp IS NULL) DESC,mistakes.loss_cp DESC",
            "rating": "MIN(games.white_elo,games.black_elo) DESC",
            "speed": "games.speed, games.time_control", "opening": "games.opening COLLATE NOCASE",
            "recent": "mistakes.detected DESC", "review": "mistakes.reviewed, mistakes.bookmarked DESC",
            "frequency": "observed DESC",
        }.get(sort, "mistakes.probability DESC,mistakes.loss_cp DESC")
        args = []
        sql = """
            SELECT mistakes.*, scan_games.scan_id, games.white, games.black, games.white_elo,
                games.black_elo, games.opening, games.eco, games.speed, jobs.state AS detail_state,
                COALESCE(position_frequency.count, 0) AS observed,
                EXISTS (SELECT 1 FROM mistakes prior WHERE prior.scan_game=mistakes.scan_game
                        AND prior.ply=mistakes.ply-1 AND prior.state IN ('verified','awaiting_maia','complete')) AS missed
            FROM mistakes
            JOIN scan_games ON scan_games.id=mistakes.scan_game
            JOIN games ON games.id=scan_games.game_id
            LEFT JOIN jobs ON jobs.stage='detail' AND jobs.target=mistakes.id
            LEFT JOIN position_frequency ON position_frequency.position=mistakes.position
                                        AND position_frequency.played=mistakes.played
            WHERE 1=1
        """
        if query:
            sql += " AND (LOWER(games.white) LIKE ? OR LOWER(games.black) LIKE ? OR LOWER(games.opening) LIKE ?)"
            args.extend([f"%{query.lower()}%"] * 3)
        if speed:
            sql += " AND games.speed=?"
            args.append(speed)
        if time_control:
            sql += " AND games.time_control=?"
            args.append(time_control)
        sql += " AND mistakes.state!='rejected'" if pending else " AND mistakes.state IN ('verified','awaiting_maia','complete')"
        if review == "bookmarked":
            sql += " AND mistakes.bookmarked=1"
        elif review in ("reviewed", "unreviewed"):
            sql += " AND mistakes.reviewed=?"
            args.append(int(review == "reviewed"))
        for op, value in (("<", clock), (">", clock_min)):
            if value is not None:
                sql += f" AND mistakes.clock{op}?"
                args.append(value)
        if eco:
            sql += " AND games.eco=?"
            args.append(eco.upper())
        return [_decoded(row, "discovery", "detail") for row in self.connection().execute(
            sql + f" ORDER BY {order},mistakes.id DESC LIMIT ? OFFSET ?", [*args, limit, offset],
        )]

    def scan_progress(self, scan_id: int) -> dict:
        db = self.connection()
        sources = [dict(r) for r in db.execute(
            "SELECT id,spec,state,seen,skipped,malformed,error FROM sources WHERE scan_id=? ORDER BY id", (scan_id,),
        )]
        for source in sources:
            source["spec"] = json.loads(source["spec"])
        jobs = {(r["stage"], r["state"]): r["total"] for r in db.execute("""
            SELECT stage,state,COUNT(*) AS total FROM jobs WHERE scan_id=? AND state!='done' GROUP BY stage,state
        """, (scan_id,))}
        totals = db.execute("SELECT analyzed,candidates,predicted FROM scans WHERE id=?", (scan_id,)).fetchone()
        if totals:
            jobs["discovery", "done"] = totals["analyzed"]
            jobs["detail", "done"] = max(0, totals["candidates"] - sum(n for (stage, _), n in jobs.items() if stage == "detail"))
            jobs["maia", "done"] = totals["predicted"]
        errors = [dict(r) for r in db.execute("""
            SELECT stage,error FROM jobs WHERE scan_id=? AND state='failed' ORDER BY id DESC LIMIT 5
        """, (scan_id,))]
        return {"sources": sources, "jobs": jobs, "errors": errors}

    def settle(self) -> None:
        db = self.connection()
        finished = db.execute("""
            SELECT id FROM scans WHERE state='running'
            AND (game_limit>0 AND selected>=game_limit OR NOT EXISTS (
                 SELECT 1 FROM sources WHERE scan_id=scans.id AND state!='done'))
            AND NOT EXISTS (SELECT 1 FROM jobs WHERE scan_id=scans.id AND state!='done')
        """).fetchall()
        if finished:
            with self.transaction() as writer:
                writer.executemany("UPDATE scans SET state='complete' WHERE id=? AND state='running'",
                                   [(r["id"],) for r in finished])
