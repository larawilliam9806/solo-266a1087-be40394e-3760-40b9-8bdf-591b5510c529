"""SQLite 存储：当前名录、备份文件登记、恢复记录。"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 当前名录（按编号可查）
CREATE TABLE IF NOT EXISTS gazetteer (
  id      TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  village TEXT NOT NULL
);
-- 上传过的备份 ZIP
CREATE TABLE IF NOT EXISTS uploads (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  filename    TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT ''
);
-- 清单登记的各正式版名录摘要
CREATE TABLE IF NOT EXISTS version_digests (
  version   TEXT NOT NULL,
  sha256    TEXT NOT NULL,
  upload_id INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
  PRIMARY KEY (version, sha256, upload_id)
);
-- 清单登记的全量文件（版号）
CREATE TABLE IF NOT EXISTS full_files (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  upload_id   INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
  version     TEXT NOT NULL,
  arcname     TEXT NOT NULL,
  stored_path TEXT,
  present     INTEGER NOT NULL DEFAULT 1
);
-- 清单登记的增量文件（起止版）
CREATE TABLE IF NOT EXISTS increments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  upload_id    INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
  from_version TEXT NOT NULL,
  to_version   TEXT NOT NULL,
  arcname      TEXT NOT NULL,
  stored_path  TEXT,
  present      INTEGER NOT NULL DEFAULT 1
);
-- 恢复记录（成功与失败都留痕）
CREATE TABLE IF NOT EXISTS recoveries (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  target_version TEXT NOT NULL,
  status         TEXT NOT NULL,           -- success / failed
  reason         TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL
);
-- 本次恢复实际使用的备份文件
CREATE TABLE IF NOT EXISTS recovery_files (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  recovery_id  INTEGER NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,             -- full / increment
  version_span TEXT NOT NULL,             -- 版号 或 起止版
  source       TEXT NOT NULL              -- 备份文件来源
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
