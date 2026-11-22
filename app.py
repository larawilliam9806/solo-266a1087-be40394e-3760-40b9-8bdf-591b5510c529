"""旧地名名录补档页：上传备份 ZIP、按目标版恢复、查询名录与恢复记录。"""
import io
import os
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from flask import (Flask, flash, redirect, render_template, request, url_for)
from werkzeug.utils import secure_filename

import db as db_module
from package import (PackageError, find_manifest_name, parse_manifest,
                     read_member, safe_member_path)
from restore import FullFile, Increment, Restorer, RestoreFailure

MAX_ROWS_ON_PAGE = 500


def create_app(test_config=None):
    app = Flask(__name__)
    data_dir = Path(__file__).resolve().parent / "data"
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        DATABASE=os.environ.get("GAZETTEER_DATABASE", str(data_dir / "app.db")),
        UPLOAD_DIR=os.environ.get("GAZETTEER_UPLOAD_DIR",
                                  str(data_dir / "uploads")),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,  # 上传 ZIP 上限
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    db_module.init_app(app)
    with app.app_context():
        db_module.init_db()

    # ------------------------------------------------------------ 页面
    @app.get("/")
    def index():
        db = db_module.get_db()
        q = request.args.get("q", "").strip()

        current_version = _meta_get(db, "current_version")
        total = db.execute("SELECT COUNT(*) AS c FROM gazetteer").fetchone()["c"]

        search_note = None
        if q:
            rows = db.execute(
                "SELECT * FROM gazetteer WHERE id = ?", (q,)).fetchall()
            if rows:
                search_note = f"编号 {q} 精确命中 {len(rows)} 行"
            else:
                rows = db.execute(
                    "SELECT * FROM gazetteer WHERE id LIKE ? ORDER BY id LIMIT 50",
                    (q + "%",)).fetchall()
                search_note = (f"编号 {q} 无精确匹配，"
                               f"前缀匹配 {len(rows)} 行" if rows
                               else f"编号 {q} 没有匹配记录")
        else:
            rows = db.execute(
                "SELECT * FROM gazetteer ORDER BY id LIMIT ?",
                (MAX_ROWS_ON_PAGE,)).fetchall()

        return render_template(
            "index.html",
            q=q, rows=rows, total=total, search_note=search_note,
            current_version=current_version,
            max_rows=MAX_ROWS_ON_PAGE,
            versions=_collect_versions(db),
            uploads=_collect_uploads(db),
            recoveries=_collect_recoveries(db),
        )

    # ------------------------------------------------------------ 上传备份 ZIP
    @app.post("/upload")
    def upload():
        f = request.files.get("file")
        if not f or not f.filename:
            flash("请选择要上传的备份 ZIP", "error")
            return redirect(url_for("index"))
        try:
            zf = zipfile.ZipFile(io.BytesIO(f.read()))
        except zipfile.BadZipFile:
            flash("上传的文件不是有效的 ZIP 压缩包", "error")
            return redirect(url_for("index"))
        try:
            manifest_name = find_manifest_name(zf)
            manifest = parse_manifest(
                read_member(zf, manifest_name).decode("utf-8"))
        except (PackageError, UnicodeDecodeError, KeyError) as exc:
            flash(f"清单解析失败：{exc}", "error")
            return redirect(url_for("index"))

        db = db_module.get_db()
        filename = secure_filename(f.filename) or "backup.zip"
        cur = db.execute(
            "INSERT INTO uploads (filename, uploaded_at) VALUES (?, ?)",
            (filename, _now()))
        upload_id = cur.lastrowid

        # 清单引用的文件一律相对清单所在目录
        base = PurePosixPath(manifest_name).parent
        dest_root = Path(app.config["UPLOAD_DIR"]) / str(upload_id)
        missing = []

        def store(arcname):
            joined = arcname if str(base) == "." else str(base / arcname)
            try:
                safe = safe_member_path(joined)
                data = read_member(zf, safe)
            except (PackageError, KeyError):
                missing.append(arcname)
                return None
            dest = dest_root / safe
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return str(dest)

        for version, arcname in manifest.fulls:
            path = store(arcname)
            db.execute(
                "INSERT INTO full_files"
                " (upload_id, version, arcname, stored_path, present)"
                " VALUES (?, ?, ?, ?, ?)",
                (upload_id, version, arcname, path, 1 if path else 0))
        for frm, to, arcname in manifest.increments:
            path = store(arcname)
            db.execute(
                "INSERT INTO increments"
                " (upload_id, from_version, to_version, arcname, stored_path, present)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (upload_id, frm, to, arcname, path, 1 if path else 0))
        for version, sha in manifest.versions:
            db.execute(
                "INSERT OR IGNORE INTO version_digests (version, sha256, upload_id)"
                " VALUES (?, ?, ?)",
                (version, sha, upload_id))

        note = (f"清单含 {len(manifest.versions)} 条正式版摘要、"
                f"{len(manifest.fulls)} 个全量文件、{len(manifest.increments)} 个增量文件")
        if missing:
            note += "；包内缺失：" + "、".join(missing)
        db.execute("UPDATE uploads SET note = ? WHERE id = ?", (note, upload_id))
        db.commit()
        flash(f"已接收备份包 {filename}：{note}",
              "warning" if missing else "success")
        return redirect(url_for("index"))

    # ------------------------------------------------------------ 恢复目标版
    @app.post("/restore")
    def restore():
        target = (request.form.get("version") or "").strip()
        if not target:
            flash("未选择目标版本", "error")
            return redirect(url_for("index"))
        db = db_module.get_db()
        restorer = _build_restorer(db)
        try:
            state, steps = restorer.restore(target)
        except RestoreFailure as exc:
            # 找不到可用链：说明原因，原名录不动，恢复记录留痕
            reason = "；".join(exc.reasons)
            db.execute(
                "INSERT INTO recoveries (target_version, status, reason, created_at)"
                " VALUES (?, 'failed', ?, ?)",
                (target, reason, _now()))
            db.commit()
            for r in exc.reasons:
                flash(f"恢复 {target} 失败：{r}", "error")
            return redirect(url_for("index"))

        # 成功：同时更新名录与本次使用的备份文件记录（同一事务）
        rows = sorted(state.values(), key=lambda r: r[0])
        cur = db.execute(
            "INSERT INTO recoveries (target_version, status, reason, created_at)"
            " VALUES (?, 'success', '', ?)",
            (target, _now()))
        recovery_id = cur.lastrowid
        for kind, label, span in steps:
            db.execute(
                "INSERT INTO recovery_files (recovery_id, kind, version_span, source)"
                " VALUES (?, ?, ?, ?)",
                (recovery_id, kind, span, label))
        db.execute("DELETE FROM gazetteer")
        db.executemany(
            "INSERT INTO gazetteer (id, name, village) VALUES (?, ?, ?)", rows)
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('current_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (target,))
        db.commit()
        flash(f"已恢复到版本 {target}：名录 {len(rows)} 行，"
              f"使用备份文件 {len(steps)} 个（见恢复记录 #{recovery_id}）", "success")
        return redirect(url_for("index"))

    return app


# ---------------------------------------------------------------- 辅助

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _meta_get(db, key):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _build_restorer(db):
    digests = {}
    for r in db.execute("SELECT version, sha256 FROM version_digests"):
        digests.setdefault(r["version"], set()).add(r["sha256"])

    def loader_for(stored_path):
        if not stored_path:
            return None

        def load():
            return Path(stored_path).read_text(encoding="utf-8")
        return load

    fulls = []
    for r in db.execute(
            "SELECT f.version, f.arcname, f.stored_path, f.present, u.filename"
            " FROM full_files f JOIN uploads u ON u.id = f.upload_id"
            " ORDER BY f.id"):
        label = f"{r['filename']} :: {r['arcname']}"
        fulls.append(FullFile(version=r["version"], label=label,
                              present=bool(r["present"]),
                              loader=loader_for(r["stored_path"])))
    increments = []
    for r in db.execute(
            "SELECT i.from_version, i.to_version, i.arcname, i.stored_path,"
            " i.present, u.filename"
            " FROM increments i JOIN uploads u ON u.id = i.upload_id"
            " ORDER BY i.id"):
        label = f"{r['filename']} :: {r['arcname']}"
        increments.append(Increment(from_version=r["from_version"],
                                    to_version=r["to_version"],
                                    label=label,
                                    present=bool(r["present"]),
                                    loader=loader_for(r["stored_path"])))
    return Restorer(digests, fulls, increments)


def _collect_versions(db):
    """汇总所有已知版本：摘要、全量/增量文件齐备情况。"""
    versions = {}

    def slot(ver):
        return versions.setdefault(ver, {
            "version": ver, "digests": set(),
            "fulls": 0, "fulls_present": 0, "inc_in": 0, "inc_out": 0})

    for r in db.execute("SELECT version, sha256 FROM version_digests"):
        slot(r["version"])["digests"].add(r["sha256"])
    for r in db.execute(
            "SELECT version, COUNT(*) c, SUM(present) p FROM full_files"
            " GROUP BY version"):
        s = slot(r["version"])
        s["fulls"], s["fulls_present"] = r["c"], r["p"] or 0
    for r in db.execute(
            "SELECT to_version v, COUNT(*) c FROM increments GROUP BY to_version"):
        slot(r["v"])["inc_in"] = r["c"]
    for r in db.execute(
            "SELECT from_version v, COUNT(*) c FROM increments GROUP BY from_version"):
        slot(r["v"])["inc_out"] = r["c"]
    return sorted(versions.values(), key=lambda v: v["version"])


def _collect_uploads(db):
    uploads = []
    for u in db.execute("SELECT * FROM uploads ORDER BY id DESC").fetchall():
        item = {"row": u, "digests": [], "fulls": [], "incs": []}
        item["digests"] = db.execute(
            "SELECT version, sha256 FROM version_digests WHERE upload_id = ?"
            " ORDER BY version", (u["id"],)).fetchall()
        item["fulls"] = db.execute(
            "SELECT version, arcname, present FROM full_files WHERE upload_id = ?"
            " ORDER BY version", (u["id"],)).fetchall()
        item["incs"] = db.execute(
            "SELECT from_version, to_version, arcname, present FROM increments"
            " WHERE upload_id = ? ORDER BY from_version, to_version",
            (u["id"],)).fetchall()
        uploads.append(item)
    return uploads


def _collect_recoveries(db):
    recs = db.execute(
        "SELECT * FROM recoveries ORDER BY id DESC LIMIT 50").fetchall()
    if not recs:
        return []
    marks = ",".join("?" * len(recs))
    files = db.execute(
        f"SELECT * FROM recovery_files WHERE recovery_id IN ({marks})"
        " ORDER BY id", [r["id"] for r in recs]).fetchall()
    by_id = {}
    for f in files:
        by_id.setdefault(f["recovery_id"], []).append(f)
    return [{"row": r, "files": by_id.get(r["id"], [])} for r in recs]


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
