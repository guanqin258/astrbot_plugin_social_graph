"""社交图谱的本地库：人、消息事件、关系。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    uid                TEXT PRIMARY KEY,
    nickname           TEXT DEFAULT '',
    cards              TEXT DEFAULT '[]',
    first_seen         INTEGER DEFAULT 0,
    last_seen          INTEGER DEFAULT 0,
    msg_count          INTEGER DEFAULT 0,
    impression         TEXT DEFAULT '',
    impression_at      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    umo          TEXT DEFAULT '',
    chat_kind    TEXT DEFAULT '',
    chat_id      TEXT DEFAULT '',
    speaker_uid  TEXT DEFAULT '',
    speaker_name TEXT DEFAULT '',
    text         TEXT DEFAULT '',
    reply_to     TEXT DEFAULT '',
    processed    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_processed ON events(processed, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS relations (
    a_uid      TEXT NOT NULL,
    b_uid      TEXT NOT NULL,
    relation   TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    evidence   TEXT DEFAULT '',
    updated_at INTEGER DEFAULT 0,
    PRIMARY KEY (a_uid, b_uid, relation)
);
"""


class SocialStore:
    def __init__(self, db_path: Path):
        self.path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------- 人 ----------------

    def touch_person(self, uid: str, nickname: str, card: str = "") -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        now = int(time.time())
        row = self.conn.execute("SELECT cards, msg_count FROM people WHERE uid=?", (uid,)).fetchone()
        if row is None:
            cards = [card] if card else []
            self.conn.execute(
                "INSERT INTO people(uid, nickname, cards, first_seen, last_seen, msg_count)"
                " VALUES(?,?,?,?,?,1)",
                (uid, nickname or "", json.dumps(cards, ensure_ascii=False), now, now),
            )
        else:
            cards = json.loads(row["cards"] or "[]")
            if card and card not in cards:
                cards.append(card)
            self.conn.execute(
                "UPDATE people SET nickname=?, cards=?, last_seen=?, msg_count=? WHERE uid=?",
                (nickname or "", json.dumps(cards, ensure_ascii=False), now, int(row["msg_count"] or 0) + 1, uid),
            )
        self.conn.commit()

    def get_person(self, uid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM people WHERE uid=?", (str(uid),)).fetchone()
        return dict(row) if row else None

    def find_people(self, keyword: str, limit: int = 8) -> list[dict[str, Any]]:
        keyword = (keyword or "").strip()
        if not keyword:
            rows = self.conn.execute(
                "SELECT * FROM people ORDER BY last_seen DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        rows = self.conn.execute(
            "SELECT * FROM people WHERE uid=? OR nickname LIKE ? OR cards LIKE ?"
            " ORDER BY last_seen DESC LIMIT ?",
            (keyword, f"%{keyword}%", f"%{keyword}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_impression(self, uid: str, text: str) -> None:
        self.conn.execute(
            "UPDATE people SET impression=?, impression_at=? WHERE uid=?",
            (text.strip(), int(time.time()), str(uid)),
        )
        self.conn.commit()

    # ---------------- 事件 ----------------

    def add_event(
        self,
        *,
        ts: int,
        umo: str,
        chat_kind: str,
        chat_id: str,
        speaker_uid: str,
        speaker_name: str,
        text: str,
        reply_to: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, umo, chat_kind, chat_id, speaker_uid, speaker_name, text, reply_to)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ts, umo, chat_kind, chat_id, speaker_uid, speaker_name, text, reply_to),
        )
        self.conn.commit()

    def unprocessed_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM events WHERE processed=0").fetchone()
        return int(row["n"] if row else 0)

    def last_event_ts(self) -> int:
        row = self.conn.execute("SELECT MAX(ts) AS t FROM events").fetchone()
        return int(row["t"] or 0) if row else 0

    def take_unprocessed(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE processed=0 ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_processed(self, ids: Iterable[int]) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        self.conn.executemany("UPDATE events SET processed=1 WHERE id=?", [(i,) for i in ids])
        self.conn.commit()

    def recent_events(self, limit: int = 30, chat_id: str = "") -> list[dict[str, Any]]:
        if chat_id:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_events_by_speaker(
        self, speaker_uid: str, since_ts: int, limit: int = 6
    ) -> list[dict[str, Any]]:
        """某个人最近说过的话（跨会话），用来回答「他今天说了什么」。"""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE speaker_uid=? AND ts>=?"
            " ORDER BY id DESC LIMIT ?",
            (str(speaker_uid), int(since_ts), limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_other_chat_events(
        self, chat_id: str, since_ts: int, limit: int = 8, only_group: bool = False
    ) -> list[dict[str, Any]]:
        """取「别的会话」里的近期消息，用于私聊时知道群里刚发生了什么。"""
        if only_group:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE chat_kind='group' AND chat_id<>? AND ts>=?"
                " ORDER BY id DESC LIMIT ?",
                (str(chat_id), int(since_ts), limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE chat_id<>? AND ts>=? ORDER BY id DESC LIMIT ?",
                (str(chat_id), int(since_ts), limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_events(self, keep_days: int = 30) -> int:
        cutoff = int(time.time()) - keep_days * 86400
        cur = self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount or 0

    # ---------------- 关系 ----------------

    def upsert_relation(
        self, a_uid: str, b_uid: str, relation: str, confidence: float, evidence: str
    ) -> None:
        a_uid, b_uid, relation = str(a_uid), str(b_uid), (relation or "").strip()
        if not a_uid or not b_uid or not relation or a_uid == b_uid:
            return
        self.conn.execute(
            "INSERT INTO relations(a_uid, b_uid, relation, confidence, evidence, updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(a_uid, b_uid, relation) DO UPDATE SET"
            " confidence=excluded.confidence, evidence=excluded.evidence, updated_at=excluded.updated_at",
            (a_uid, b_uid, relation, float(confidence), evidence[:500], int(time.time())),
        )
        self.conn.commit()

    def relations_of(self, uid: str, min_conf: float = 0.45, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM relations WHERE (a_uid=? OR b_uid=?) AND confidence>=?"
            " ORDER BY confidence DESC LIMIT ?",
            (str(uid), str(uid), min_conf, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_relations(self, min_conf: float = 0.45, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM relations WHERE confidence>=? ORDER BY confidence DESC LIMIT ?",
            (min_conf, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 页面用：增删改查 ----------------

    def list_people(self, keyword: str = "", limit: int = 200) -> list[dict[str, Any]]:
        keyword = (keyword or "").strip()
        if keyword:
            rows = self.conn.execute(
                "SELECT * FROM people WHERE uid LIKE ? OR nickname LIKE ? OR impression LIKE ?"
                " ORDER BY msg_count DESC LIMIT ?",
                (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM people ORDER BY msg_count DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_person(self, uid: str, nickname: str | None = None, impression: str | None = None) -> bool:
        row = self.conn.execute("SELECT uid FROM people WHERE uid=?", (str(uid),)).fetchone()
        if row is None:
            return False
        sets, args = [], []
        if nickname is not None:
            sets.append("nickname=?")
            args.append(str(nickname).strip())
        if impression is not None:
            sets.append("impression=?")
            args.append(str(impression).strip())
            sets.append("impression_at=?")
            args.append(int(time.time()))
        if not sets:
            return True
        args.append(str(uid))
        self.conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE uid=?", tuple(args))
        self.conn.commit()
        return True

    def delete_person(self, uid: str, drop_events: bool = True) -> int:
        uid = str(uid)
        self.conn.execute("DELETE FROM relations WHERE a_uid=? OR b_uid=?", (uid, uid))
        n = 0
        if drop_events:
            cur = self.conn.execute("DELETE FROM events WHERE speaker_uid=?", (uid,))
            n = cur.rowcount or 0
        self.conn.execute("DELETE FROM people WHERE uid=?", (uid,))
        self.conn.commit()
        return n

    def delete_relation(self, a_uid: str, b_uid: str, relation: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM relations WHERE a_uid=? AND b_uid=? AND relation=?",
            (str(a_uid), str(b_uid), str(relation)),
        )
        self.conn.commit()
        return cur.rowcount or 0

    def events_of_speaker(self, uid: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE speaker_uid=? ORDER BY id DESC LIMIT ?",
            (str(uid), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_feed(self, limit: int = 60, chat_id: str = "") -> list[dict[str, Any]]:
        if chat_id:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (str(chat_id), limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def stats(self) -> dict[str, int]:
        def one(sql: str) -> int:
            row = self.conn.execute(sql).fetchone()
            return int(row[0] or 0) if row else 0

        return {
            "people": one("SELECT COUNT(*) FROM people"),
            "with_impression": one("SELECT COUNT(*) FROM people WHERE impression<>''"),
            "events": one("SELECT COUNT(*) FROM events"),
            "unprocessed": one("SELECT COUNT(*) FROM events WHERE processed=0"),
            "relations": one("SELECT COUNT(*) FROM relations"),
        }

    def all_relations_raw(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM relations ORDER BY confidence DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
