"""Schema + helpers for the resource database (data/resource.db).

Three levels:
  resources      - one row per upstream corpus (license, source url, stats)
  voices         - one row per distinct speaker ("音色") harvested from a corpus
  voice_samples  - one row per reference clip; `verified` is written only by
                   verify_samples.py

The column names mirror backend/models/voice.py (VoiceProfile) so that
import_to_voices.py can map them one-to-one.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS resources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key      TEXT UNIQUE NOT NULL,
    provider        TEXT,
    dataset         TEXT,
    source_url      TEXT,
    page_url        TEXT,
    license         TEXT,
    license_url     TEXT,
    attribution     TEXT,
    languages       TEXT,
    harvest_kind    TEXT,
    status          TEXT DEFAULT 'pending',
    note            TEXT,
    files_seen      INTEGER DEFAULT 0,
    bytes_downloaded INTEGER DEFAULT 0,
    sample_count    INTEGER DEFAULT 0,
    discovered_at   TEXT,
    last_sync_at    TEXT
);

CREATE TABLE IF NOT EXISTS voices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    voice_key       TEXT UNIQUE NOT NULL,
    resource_id     INTEGER REFERENCES resources(id),
    source_key      TEXT,
    speaker_id      TEXT,
    name_hint       TEXT,
    gender          TEXT,
    age_band        TEXT,
    language        TEXT,
    style           TEXT,
    emotion         TEXT,
    attr_source     TEXT,
    sample_count    INTEGER DEFAULT 0,
    verified_count  INTEGER DEFAULT 0,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS voice_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT UNIQUE NOT NULL,
    voice_id        INTEGER REFERENCES voices(id),
    resource_id     INTEGER REFERENCES resources(id),
    source_key      TEXT,
    speaker_id      TEXT,
    gender          TEXT,
    age_band        TEXT,
    language        TEXT,
    style           TEXT,
    emotion         TEXT,
    path            TEXT,
    path_22k        TEXT,
    sample_rate     INTEGER,
    channels        INTEGER,
    duration_s      REAL,
    rms             REAL,
    peak            REAL,
    noise_floor     REAL,
    voiced_ratio    REAL,
    f0_median       REAL,
    clipping        INTEGER DEFAULT 0,
    src_sample_rate INTEGER,
    src_format      TEXT,
    license         TEXT,
    source_url      TEXT,
    page_url        TEXT,
    manifest_path   TEXT,
    verified        INTEGER DEFAULT 0,
    verify_note     TEXT,
    created_at      TEXT,
    verified_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_samples_voice ON voice_samples(voice_id);
CREATE INDEX IF NOT EXISTS idx_samples_verified ON voice_samples(verified);
CREATE INDEX IF NOT EXISTS idx_samples_attr ON voice_samples(gender, language, style);

CREATE TABLE IF NOT EXISTS run_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    script  TEXT,
    action  TEXT,
    target  TEXT,
    status  TEXT,
    bytes   INTEGER DEFAULT 0,
    note    TEXT
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ResourceDB:
    def __init__(self, path):
        self.path = str(path)
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- resources ----------
    def upsert_resource(self, rec):
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM resources WHERE source_key=?", (rec["source_key"],))
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE resources SET provider=?,dataset=?,source_url=?,page_url=?,
                        license=?,license_url=?,attribution=?,languages=?,harvest_kind=?,
                        status=?,note=?,files_seen=?,bytes_downloaded=?,sample_count=?,
                        last_sync_at=? WHERE id=?""",
                        (rec.get("provider"), rec.get("dataset"), rec.get("source_url"),
                         rec.get("page_url"), rec.get("license"), rec.get("license_url"),
                         rec.get("attribution"), rec.get("languages"), rec.get("harvest_kind"),
                         rec.get("status", "pending"), rec.get("note"),
                         rec.get("files_seen", 0), rec.get("bytes_downloaded", 0),
                         rec.get("sample_count", 0), now(), row["id"]))
            cur.execute("SELECT sample_count FROM resources WHERE id=?", (row["id"],))
            return row["id"]
        cur.execute("""INSERT INTO resources
                       (source_key,provider,dataset,source_url,page_url,license,license_url,
                        attribution,languages,harvest_kind,status,note,files_seen,
                        bytes_downloaded,sample_count,discovered_at,last_sync_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rec["source_key"], rec.get("provider"), rec.get("dataset"),
                     rec.get("source_url"), rec.get("page_url"), rec.get("license"),
                     rec.get("license_url"), rec.get("attribution"), rec.get("languages"),
                     rec.get("harvest_kind"), rec.get("status", "pending"), rec.get("note"),
                     rec.get("files_seen", 0), rec.get("bytes_downloaded", 0),
                     rec.get("sample_count", 0), now(), now()))
        return cur.lastrowid

    def touch_resource(self, rid, status=None, note=None, files_seen=None,
                       bytes_downloaded=None, sample_count=None):
        sets, args = ["last_sync_at=?"], [now()]
        for col, val in (("status", status), ("note", note), ("files_seen", files_seen),
                         ("bytes_downloaded", bytes_downloaded), ("sample_count", sample_count)):
            if val is not None:
                sets.append("%s=?" % col)
                args.append(val)
        args.append(rid)
        self.conn.execute("UPDATE resources SET %s WHERE id=?" % ",".join(sets), args)
        self.conn.commit()

    def get_resource(self, key):
        return self.conn.execute("SELECT * FROM resources WHERE source_key=?", (key,)).fetchone()

    # ---------- voices ----------
    def upsert_voice(self, v):
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM voices WHERE voice_key=?", (v["voice_key"],))
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE voices SET resource_id=?,source_key=?,speaker_id=?,name_hint=?,
                           gender=?,age_band=?,language=?,style=?,emotion=?,attr_source=?,
                           sample_count=?,verified_count=? WHERE id=?""",
                        (v.get("resource_id"), v.get("source_key"), v.get("speaker_id"),
                         v.get("name_hint"), v.get("gender"), v.get("age_band"),
                         v.get("language"), v.get("style"), v.get("emotion"),
                         v.get("attr_source"), v.get("sample_count", 0),
                         v.get("verified_count", 0), row["id"]))
            return row["id"]
        cur.execute("""INSERT INTO voices
                       (voice_key,resource_id,source_key,speaker_id,name_hint,gender,age_band,
                        language,style,emotion,attr_source,sample_count,verified_count,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (v["voice_key"], v.get("resource_id"), v.get("source_key"),
                     v.get("speaker_id"), v.get("name_hint"), v.get("gender"),
                     v.get("age_band"), v.get("language"), v.get("style"), v.get("emotion"),
                     v.get("attr_source"), v.get("sample_count", 0), v.get("verified_count", 0),
                     now()))
        return cur.lastrowid

    def get_voice(self, key):
        return self.conn.execute("SELECT * FROM voices WHERE voice_key=?", (key,)).fetchone()

    # ---------- samples ----------
    def sample_exists(self, uid):
        return self.conn.execute(
            "SELECT id, verified, path FROM voice_samples WHERE uid=?", (uid,)).fetchone()

    def insert_sample(self, s):
        cols = ["uid", "voice_id", "resource_id", "source_key", "speaker_id", "gender",
                "age_band", "language", "style", "emotion", "path", "path_22k", "sample_rate",
                "channels", "duration_s", "rms", "peak", "noise_floor", "voiced_ratio",
                "f0_median", "clipping", "src_sample_rate", "src_format", "license",
                "source_url", "page_url", "manifest_path", "verified", "verify_note",
                "created_at"]
        vals = [s.get(c) for c in cols]
        self.conn.execute("INSERT OR REPLACE INTO voice_samples (%s) VALUES (%s)"
                          % (",".join(cols), ",".join("?" * len(cols))), vals)
        self.conn.commit()
        return self.conn.execute("SELECT id FROM voice_samples WHERE uid=?",
                                 (s["uid"],)).fetchone()["id"]

    def set_verified(self, sid, verified, note=""):
        self.conn.execute("UPDATE voice_samples SET verified=?, verify_note=?, verified_at=? "
                          "WHERE id=?", (int(verified), note, now(), sid))
        self.conn.commit()

    def recompute_voice_counts(self):
        self.conn.execute("""UPDATE voices SET sample_count =
                              (SELECT COUNT(*) FROM voice_samples WHERE voice_samples.voice_id=voices.id)""")
        self.conn.execute("""UPDATE voices SET verified_count =
                              (SELECT COUNT(*) FROM voice_samples WHERE voice_samples.voice_id=voices.id
                               AND voice_samples.verified=1)""")
        self.conn.execute("""UPDATE resources SET sample_count =
                              (SELECT COUNT(*) FROM voice_samples WHERE voice_samples.resource_id=resources.id)""")
        self.conn.commit()

    def log(self, script, action, target, status, bytes_=0, note=""):
        self.conn.execute("INSERT INTO run_log (ts,script,action,target,status,bytes,note) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (now(), script, action, target, status, bytes_, note[:500]))
        self.conn.commit()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None


def write_manifest_line(path, obj):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
