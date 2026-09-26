"""SQLite 数据访问层。

表结构
------
pieces       乐曲与文件、任务状态
score_notes  展开反复后的期望音符（含原谱坐标 + 展开后 beat 坐标）
obs_notes    从录音提取的观测音符（含基频、起音、置信状态）
alignments   一次对齐结果版本（条目整体 JSON 存储，便于确定性导出与回放）
anchors      用户锚点：lock（锁定配对）/exclude_score/exclude_obs，
             以及 resolve（待确认片段的处置结果）
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.environ.get("DATA_DIR", "/data"), "review.db"))

_lock = threading.RLock()


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS pieces (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    musicxml_path   TEXT NOT NULL,
    audio_path      TEXT,
    status          TEXT NOT NULL DEFAULT 'created',
        -- created / analyzed / failed
    error           TEXT,
    duration_s      REAL,
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    -- 谱面修订迁移：该乐曲是某条已确认修订的新复盘入口
    revision_of     TEXT REFERENCES pieces(id),
    revision_id     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS score_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id        TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    midi            REAL NOT NULL,
    beat            REAL NOT NULL,      -- 展开后四分音符位置
    dur_q           REAL NOT NULL,      -- 四分音符时长
    measure         INTEGER,            -- 原谱小节号
    beat_in_measure REAL,               -- 原谱小节内拍位
    occurrence      INTEGER,            -- 反复出现序号（0 基）
    part            INTEGER,
    UNIQUE(piece_id, idx)
);

CREATE TABLE IF NOT EXISTS obs_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id        TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    midi            REAL,
    onset           REAL NOT NULL,      -- 秒
    duration        REAL NOT NULL,      -- 秒
    state           TEXT NOT NULL,      -- stable / poly / silent
    voiced_ratio    REAL,
    poly_ratio      REAL,
    onset_strength  REAL,
    UNIQUE(piece_id, idx)
);

CREATE TABLE IF NOT EXISTS alignments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id        TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(piece_id, version)
);

CREATE TABLE IF NOT EXISTS anchors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id        TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,      -- lock/exclude_score/exclude_obs
    score_index     INTEGER,
    obs_index       INTEGER,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(piece_id, kind, score_index, obs_index)
);

CREATE TABLE IF NOT EXISTS comparisons (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    baseline_piece  TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    refreshed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS comparison_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id       TEXT NOT NULL REFERENCES comparisons(id) ON DELETE CASCADE,
    piece_id            TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    is_baseline         INTEGER NOT NULL DEFAULT 0,
    ord                 INTEGER NOT NULL,
    alignment_version   INTEGER NOT NULL,  -- 固化时的对齐版本（只读快照）
    musicxml_sha256     TEXT,
    wav_sha256          TEXT,
    fingerprint         TEXT NOT NULL,     -- 乐谱指纹（展开序列+原谱坐标）
    snapshot_json       TEXT NOT NULL,     -- 固化后的按谱音结果（只读）
    UNIQUE(comparison_id, piece_id)
);

CREATE TABLE IF NOT EXISTS synthesis_tasks (
    id              TEXT PRIMARY KEY,
    comparison_id   TEXT NOT NULL UNIQUE
                    REFERENCES comparisons(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    -- draft（已规划未渲染）/ built（WAV 已固化）/ blocked（存在无合格来源区间）
    status          TEXT NOT NULL DEFAULT 'draft',
    params_json     TEXT NOT NULL,         -- 代价权重等公开参数快照
    overrides_json  TEXT NOT NULL DEFAULT '{}',  -- 钉住/禁用（按谱位）
    plan_json       TEXT NOT NULL,         -- 最新规划：选源/切点/代价/阻塞
    manifest_json   TEXT,                  -- 仅 built：确定性清单（含固化哈希）
    wav_path        TEXT,
    wav_sha256      TEXT,
    built_at        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- 偏差练习处方与复测
--   处方是只读对象：创建时固化谱指纹与对齐版本/规则，状态只按
--   created -> retested -> voided 单向推进（created 也可直接作废）。
CREATE TABLE IF NOT EXISTS prescriptions (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    -- piece：从单次已分析乐曲框选；comparison：从未过期多次演奏对比框选
    source_type     TEXT NOT NULL,
    source_piece_id TEXT REFERENCES pieces(id) ON DELETE SET NULL,
    comparison_id   TEXT REFERENCES comparisons(id) ON DELETE SET NULL,
    fingerprint     TEXT NOT NULL,         -- 固化的乐谱指纹（只读）
    -- created（待复测）/ retested（已有复测结果）/ voided（已作废）
    status          TEXT NOT NULL DEFAULT 'created',
    rules_json      TEXT NOT NULL,         -- 逐段规则 + 公开参数（只读）
    frozen_json     TEXT NOT NULL,         -- 固化的来源对齐版本/哈希（只读）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    voided_at       TEXT,
    latest_retest_id TEXT
);

CREATE TABLE IF NOT EXISTS prescription_retests (
    id                  TEXT PRIMARY KEY,
    prescription_id     TEXT NOT NULL
                        REFERENCES prescriptions(id) ON DELETE CASCADE,
    ord                 INTEGER NOT NULL,  -- 该处方第几次复测（从 1 起）
    target_piece_id     TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    target_title        TEXT NOT NULL,
    target_version      INTEGER NOT NULL,  -- 复测目标固化的对齐版本
    target_wav_sha256   TEXT,
    source_version_id   INTEGER,           -- 依据的处方固化版本（重绑历史）
    result_json         TEXT NOT NULL,     -- 逐段/逐规则/证据音 确定性结果
    overall             TEXT NOT NULL,     -- pass / fail / undeterminable
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prescription_source_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prescription_id TEXT NOT NULL REFERENCES prescriptions(id) ON DELETE CASCADE,
    ord             INTEGER NOT NULL,      -- 0=创建时固化，之后显式重绑递增
    source_type     TEXT NOT NULL,
    source_piece_id TEXT,
    comparison_id   TEXT,
    frozen_json     TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- request_id 幂等：同一 request_id 的重复提交直接回放首次结果
CREATE TABLE IF NOT EXISTS idempotent_requests (
    request_id      TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,         -- create_prescription / retest
    prescription_id TEXT,
    status_code     INTEGER NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- 偏差片段练习谱
--   从单次复盘按“展开后的整小节 + 反复轮次”框选连续片段生成。
--   任务行只保存片段参数与**当前版本指针**（current_version）；
--   每次成功生成（创建 / 显式重建）都在 practice_score_versions
--   追加一个**只读版本**：独立文件、固化清单与来源版本指针。
--   重建失败不新增版本、不改写指针；旧版本（含过期稿）始终可下载；
--   删除任务级联清理全部版本。
CREATE TABLE IF NOT EXISTS practice_scores (
    id              TEXT PRIMARY KEY,
    piece_id        TEXT NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    -- draft（仅创建过程中的瞬时态）/ built（至少有一个固化版本）
    status          TEXT NOT NULL DEFAULT 'draft',
    segments_json   TEXT NOT NULL,  -- 片段（整小节+反复轮次）+ 循环/速度
    params_json     TEXT NOT NULL,  -- 公开生成参数快照
    current_version INTEGER,        -- 当前版本指针（practice_score_versions.version）
    built_at        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS practice_score_versions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                 TEXT NOT NULL
                            REFERENCES practice_scores(id) ON DELETE CASCADE,
    version                 INTEGER NOT NULL,  -- 1 基；每次成功生成 +1
    -- built：已原子落盘并固化（版本一旦写入即为只读，不再改写）
    status                  TEXT NOT NULL DEFAULT 'built',
    -- 该版本固化的来源版本（之后来源变化只把该版本标 stale，绝不改写）
    source_musicxml_sha256  TEXT,
    alignment_version       INTEGER,
    manifest_json           TEXT NOT NULL,     -- 固化清单（只读）
    musicxml_path           TEXT NOT NULL,     -- 版本独立文件，不互相覆盖
    musicxml_sha256         TEXT NOT NULL,     -- 该版本生成文件本身的 sha256
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id, version)
);

-- ------------------------------------------------------------
-- 谱面修订迁移
--   preview：用户已上传修订谱并生成确定性迁移预览，尚未确认（不产生新
--            复盘入口；可删除后重新上传）；
--   committed：用户显式确认，创建了供后续录音使用的新复盘入口 piece。
--   旧录音 piece 与全部历史产物一律只读，迁移只追加新记录，绝不改写。
--   提交时的逐谱位匹配 / 逐项继承判定 / 可继承处方与练习谱模板整体固化。
CREATE TABLE IF NOT EXISTS score_revisions (
    id                  TEXT PRIMARY KEY,
    source_piece_id     TEXT NOT NULL REFERENCES pieces(id),
    new_piece_id        TEXT REFERENCES pieces(id),  -- committed 后存在
    title               TEXT NOT NULL,
    -- preview（仅预览）/ committed（已确认并创建新复盘入口）
    status              TEXT NOT NULL DEFAULT 'preview',
    source_musicxml_sha256  TEXT NOT NULL,
    revised_musicxml_sha256 TEXT NOT NULL,
    source_alignment_version INTEGER,
    revised_path        TEXT NOT NULL,   -- 修订版 MusicXML 落盘路径
    -- 是否存在阻止提交的匹配冲突 / 多声部歧义（预览阶段也允许保存）
    has_blockers        INTEGER NOT NULL DEFAULT 0,
    blockers_json       TEXT NOT NULL DEFAULT '[]',
    -- 确定性匹配报告（逐谱位 + 逐项继承判定，只读快照）
    mapping_json        TEXT NOT NULL,
    -- 提交时实际继承的锚点摘要（commit 后填充）
    inherited_json      TEXT,
    -- 待新录音分析完成后一键接续的处方 / 练习谱模板
    carryover_templates_json TEXT,
    committed_at        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 谱面修订提交幂等：同一 request_id 重复提交回放首次响应
CREATE TABLE IF NOT EXISTS revision_idempotency (
    request_id      TEXT PRIMARY KEY,
    revision_id     TEXT,
    status_code     INTEGER NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_practice_versions(conn)
        _migrate_revision_entry(conn)


def _migrate_revision_entry(conn):
    """旧库升级：pieces 增加谱面修订迁移入口列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pieces)")}
    if "revision_of" not in cols:
        conn.execute("ALTER TABLE pieces ADD COLUMN revision_of TEXT")
    if "revision_id" not in cols:
        conn.execute("ALTER TABLE pieces ADD COLUMN revision_id TEXT")


def _migrate_practice_versions(conn):
    """旧库升级：practice_scores 增加 current_version，并把已 built 旧稿
    回填为 version=1 的只读版本（旧库每个任务只有一份固定文件）。"""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(practice_scores)")}
    if "current_version" not in cols:
        conn.execute(
            "ALTER TABLE practice_scores ADD COLUMN current_version INTEGER")
    # 仅旧库（任务行上仍有固化列）需要回填；新建库这些列不存在，直接跳过。
    if "musicxml_path" not in cols or "plan_json" not in cols:
        return
    # 旧库中 source_musicxml_sha256 / alignment_version / plan_json /
    # musicxml_path / musicxml_sha256 / built_at 仍留在任务行上；
    # 已 built 且尚未回填的任务整体搬迁为 version 1。
    conn.execute(
        """
        INSERT INTO practice_score_versions
            (task_id, version, status, source_musicxml_sha256,
             alignment_version, manifest_json, musicxml_path,
             musicxml_sha256, created_at)
        SELECT id, 1, 'built', source_musicxml_sha256, alignment_version,
               plan_json, musicxml_path, COALESCE(musicxml_sha256, ''),
               COALESCE(built_at, datetime('now'))
        FROM practice_scores
        WHERE status = 'built' AND musicxml_path IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM practice_score_versions v
              WHERE v.task_id = practice_scores.id)
        """)
    conn.execute(
        """
        UPDATE practice_scores SET current_version = 1
        WHERE current_version IS NULL AND status = 'built'
          AND EXISTS (SELECT 1 FROM practice_score_versions v
                      WHERE v.task_id = practice_scores.id
                        AND v.version = 1)
        """)


# ------------------------------------------------------------ pieces

def create_piece(piece_id: str, title: str, musicxml_path: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO pieces (id, title, musicxml_path) VALUES (?,?,?)",
            (piece_id, title, musicxml_path))


def save_analysis(piece_id: str, audio_path: str, duration: float,
                  score_notes, obs_notes, warnings, peaks,
                  status="analyzed", error=None):
    """一次性写入解析 + 分析结果（事务）。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM score_notes WHERE piece_id=?", (piece_id,))
        conn.execute("DELETE FROM obs_notes WHERE piece_id=?", (piece_id,))
        conn.executemany(
            """INSERT INTO score_notes
               (piece_id, idx, midi, beat, dur_q, measure,
                beat_in_measure, occurrence, part)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(piece_id, n["index"], n["midi"], n["beat"], n["dur_q"],
              n.get("measure"), n.get("beat_in_measure"),
              n.get("occurrence"), n.get("part", 0)) for n in score_notes])
        conn.executemany(
            """INSERT INTO obs_notes
               (piece_id, idx, midi, onset, duration, state,
                voiced_ratio, poly_ratio, onset_strength)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(piece_id, o["index"], o.get("midi"), o["onset"],
              o["duration"], o["state"], o.get("voiced_ratio"),
              o.get("poly_ratio"), o.get("onset_strength"))
             for o in obs_notes])
        conn.execute(
            """UPDATE pieces SET audio_path=?, duration_s=?,
               warnings_json=?, status=?, error=? WHERE id=?""",
            (audio_path, duration,
             json.dumps({"warnings": warnings, "peaks": peaks},
                        ensure_ascii=False),
             status, error, piece_id))


def get_piece(piece_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pieces WHERE id=?",
                           (piece_id,)).fetchone()
        return dict(row) if row else None


def list_pieces():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, status, duration_s, created_at "
            "FROM pieces ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_score_notes(piece_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM score_notes WHERE piece_id=? ORDER BY idx",
            (piece_id,)).fetchall()
        return [dict(r) for r in rows]


def get_obs_notes(piece_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM obs_notes WHERE piece_id=? ORDER BY idx",
            (piece_id,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------ anchors

def get_anchors(piece_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM anchors WHERE piece_id=? ORDER BY id",
            (piece_id,)).fetchall()
        return [dict(r) for r in rows]


def add_anchor(piece_id: str, kind: str, score_index, obs_index,
               note=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO anchors
               (piece_id, kind, score_index, obs_index, note)
               VALUES (?,?,?,?,?)""",
            (piece_id, kind, score_index, obs_index, note))


def delete_anchor(piece_id: str, anchor_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM anchors WHERE id=? AND piece_id=?",
                           (anchor_id, piece_id))
        return cur.rowcount


def clear_anchors(piece_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM anchors WHERE piece_id=?", (piece_id,))


# ------------------------------------------------------------ alignments

def save_alignment(piece_id: str, result_json: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM alignments "
            "WHERE piece_id=?", (piece_id,)).fetchone()
        version = row["v"] + 1
        conn.execute(
            "INSERT INTO alignments (piece_id, version, result_json) "
            "VALUES (?,?,?)", (piece_id, version, result_json))
        return version


def get_latest_alignment(piece_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM alignments WHERE piece_id=? "
            "ORDER BY version DESC LIMIT 1", (piece_id,)).fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------ 多次演奏对比

def create_comparison(cmp_id: str, title: str, baseline_piece: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO comparisons (id, title, baseline_piece) "
            "VALUES (?,?,?)", (cmp_id, title, baseline_piece))


def delete_comparison(cmp_id: str):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM comparisons WHERE id=?", (cmp_id,))
        return cur.rowcount


def touch_comparison(cmp_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE comparisons SET refreshed_at=datetime('now') "
            "WHERE id=?", (cmp_id,))


def list_comparisons():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM comparisons ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_comparison(cmp_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM comparisons WHERE id=?",
                           (cmp_id,)).fetchone()
        return dict(row) if row else None


def get_snapshots(cmp_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_snapshots WHERE comparison_id=? "
            "ORDER BY ord", (cmp_id,)).fetchall()
        return [dict(r) for r in rows]


def get_snapshot(cmp_id: str, piece_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM comparison_snapshots WHERE comparison_id=? "
            "AND piece_id=?", (cmp_id, piece_id)).fetchone()
        return dict(row) if row else None


def upsert_snapshot(cmp_id: str, piece_id: str, is_baseline: bool,
                    ord_: int, alignment_version: int,
                    musicxml_sha256, wav_sha256, fingerprint: str,
                    snapshot_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO comparison_snapshots
               (comparison_id, piece_id, is_baseline, ord,
                alignment_version, musicxml_sha256, wav_sha256,
                fingerprint, snapshot_json)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(comparison_id, piece_id) DO UPDATE SET
                 is_baseline=excluded.is_baseline,
                 ord=excluded.ord,
                 alignment_version=excluded.alignment_version,
                 musicxml_sha256=excluded.musicxml_sha256,
                 wav_sha256=excluded.wav_sha256,
                 fingerprint=excluded.fingerprint,
                 snapshot_json=excluded.snapshot_json""",
            (cmp_id, piece_id, 1 if is_baseline else 0, ord_,
             alignment_version, musicxml_sha256, wav_sha256,
             fingerprint, snapshot_json))


def comparisons_referencing_piece(piece_id: str):
    """返回引用了某录音的对比 id（用于删除录音场景的级联/提示）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT comparison_id FROM comparison_snapshots WHERE piece_id=?",
            (piece_id,)).fetchall()
        return [r["comparison_id"] for r in rows]


# ------------------------------------------------------------ 最佳片段合成轨

def create_synthesis_task(task_id: str, cmp_id: str, title: str,
                          params_json: str, overrides_json: str,
                          plan_json: str, status: str = "draft"):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO synthesis_tasks
               (id, comparison_id, title, status, params_json,
                overrides_json, plan_json)
               VALUES (?,?,?,?,?,?,?)""",
            (task_id, cmp_id, title, status, params_json, overrides_json,
             plan_json))


def get_synthesis_task(task_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM synthesis_tasks WHERE id=?",
                           (task_id,)).fetchone()
        return dict(row) if row else None


def get_synthesis_task_by_comparison(cmp_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM synthesis_tasks WHERE comparison_id=?",
            (cmp_id,)).fetchone()
        return dict(row) if row else None


def update_synthesis_plan(task_id: str, params_json: str,
                          overrides_json: str, plan_json: str,
                          status: str):
    """更新规划结果（用于从未固化的任务）。

    会清空既有固化字段；**已 built 任务不得调用本函数**——钉住/禁用后
    即使新规划 blocked，旧 WAV / 清单 / 固化版本也必须保留，
    请改用 :func:`set_synthesis_plan_keep_built`。
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE synthesis_tasks SET
               params_json=?, overrides_json=?, plan_json=?, status=?,
               manifest_json=NULL, wav_path=NULL, wav_sha256=NULL,
               built_at=NULL
             WHERE id=?""",
            (params_json, overrides_json, plan_json, status, task_id))


def set_synthesis_plan_keep_built(task_id: str, params_json: str,
                                  overrides_json: str, plan_json: str):
    """只更新规划（params/overrides/plan_json），**钉住固化产物**。

    用于已 built 任务施加钉住/禁用（含新规划 blocked）的场景：
    status 保持 'built'，manifest_json / wav_path / wav_sha256 /
    built_at 原样保留，试听与下载能力不受影响。旧产物只在之后显式
    重建成功时才被原子替换（见 :func:`mark_synthesis_built`）。
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE synthesis_tasks SET
               params_json=?, overrides_json=?, plan_json=?
             WHERE id=?""",
            (params_json, overrides_json, plan_json, task_id))


def mark_synthesis_built(task_id: str, manifest_json: str, wav_path: str,
                         wav_sha256: str):
    with get_conn() as conn:
        conn.execute(
            """UPDATE synthesis_tasks
               SET status='built', manifest_json=?, wav_path=?,
                   wav_sha256=?, built_at=datetime('now')
             WHERE id=?""",
            (manifest_json, wav_path, wav_sha256, task_id))


def delete_synthesis_task(task_id: str):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM synthesis_tasks WHERE id=?",
                           (task_id,))
        return cur.rowcount


# ------------------------------------------------------------ 偏差练习处方

def create_prescription(prx_id: str, title: str, source_type: str,
                        source_piece_id, comparison_id, fingerprint: str,
                        rules_json: str, frozen_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO prescriptions
               (id, title, source_type, source_piece_id, comparison_id,
                fingerprint, rules_json, frozen_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (prx_id, title, source_type, source_piece_id, comparison_id,
             fingerprint, rules_json, frozen_json))


def get_prescription(prx_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM prescriptions WHERE id=?",
                           (prx_id,)).fetchone()
        return dict(row) if row else None


def list_prescriptions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM prescriptions ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def set_prescription_status(prx_id: str, status: str,
                            latest_retest_id=None, voided=False):
    with get_conn() as conn:
        conn.execute(
            """UPDATE prescriptions SET status=?,
                   latest_retest_id=COALESCE(?, latest_retest_id),
                   voided_at=CASE WHEN ? THEN datetime('now') ELSE voided_at END
             WHERE id=?""",
            (status, latest_retest_id, 1 if voided else 0, prx_id))


def update_prescription_frozen(prx_id: str, frozen_json: str):
    """显式重绑：替换当前固化的来源版本（历史保留在 source_versions）。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE prescriptions SET frozen_json=? WHERE id=?",
            (frozen_json, prx_id))


def add_prescription_source_version(prx_id: str, ord_: int, source_type: str,
                                    source_piece_id, comparison_id,
                                    frozen_json: str, note=""):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO prescription_source_versions
               (prescription_id, ord, source_type, source_piece_id,
                comparison_id, frozen_json, note)
               VALUES (?,?,?,?,?,?,?)""",
            (prx_id, ord_, source_type, source_piece_id, comparison_id,
             frozen_json, note))
        return cur.lastrowid


def get_prescription_source_versions(prx_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM prescription_source_versions WHERE prescription_id=? "
            "ORDER BY ord", (prx_id,)).fetchall()
        return [dict(r) for r in rows]


def next_retest_ord(prx_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ord),0) AS o FROM prescription_retests "
            "WHERE prescription_id=?", (prx_id,)).fetchone()
        return row["o"] + 1


def create_retest(retest_id: str, prx_id: str, ord_: int, target_piece_id: str,
                  target_title: str, target_version: int,
                  target_wav_sha256, source_version_id, result_json: str,
                  overall: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO prescription_retests
               (id, prescription_id, ord, target_piece_id, target_title,
                target_version, target_wav_sha256, source_version_id,
                result_json, overall)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (retest_id, prx_id, ord_, target_piece_id, target_title,
             target_version, target_wav_sha256, source_version_id,
             result_json, overall))


def get_retest(retest_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM prescription_retests WHERE id=?",
                           (retest_id,)).fetchone()
        return dict(row) if row else None


def list_retests(prx_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM prescription_retests WHERE prescription_id=? "
            "ORDER BY ord", (prx_id,)).fetchall()
        return [dict(r) for r in rows]


def get_idempotent(request_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM idempotent_requests WHERE request_id=?",
            (request_id,)).fetchone()
        return dict(row) if row else None


def save_idempotent(request_id: str, scope: str, prx_id, status_code: int,
                    response_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO idempotent_requests
               (request_id, scope, prescription_id, status_code, response_json)
               VALUES (?,?,?,?,?)""",
            (request_id, scope, prx_id, status_code, response_json))


# ------------------------------------------------------------ 偏差片段练习谱

def create_practice_score(task_id: str, piece_id: str, title: str,
                          segments_json: str, params_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO practice_scores
               (id, piece_id, title, status, segments_json, params_json)
               VALUES (?,?,?,'draft',?,?)""",
            (task_id, piece_id, title, segments_json, params_json))


def get_practice_score(task_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM practice_scores WHERE id=?",
                           (task_id,)).fetchone()
        return dict(row) if row else None


def list_practice_scores():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM practice_scores ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_practice_version(task_id: str, version: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM practice_score_versions "
            "WHERE task_id=? AND version=?",
            (task_id, version)).fetchone()
        return dict(row) if row else None


def get_current_practice_version(task_id: str):
    """当前版本指针指向的只读版本行（无版本返回 None）。"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT v.* FROM practice_scores t
               JOIN practice_score_versions v
                 ON v.task_id = t.id AND v.version = t.current_version
               WHERE t.id=?""",
            (task_id,)).fetchone()
        return dict(row) if row else None


def list_practice_versions(task_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM practice_score_versions WHERE task_id=? "
            "ORDER BY version DESC", (task_id,)).fetchall()
        return [dict(r) for r in rows]


def next_practice_version(task_id: str) -> int:
    """该任务下一个版本号（当前最大 +1，无版本时为 1）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v "
            "FROM practice_score_versions WHERE task_id=?",
            (task_id,)).fetchone()
        return row["v"] + 1


def add_practice_version(task_id: str, version: int,
                         source_musicxml_sha256,
                         alignment_version, manifest_json: str,
                         musicxml_path: str, musicxml_sha256: str):
    """追加一个只读版本并把任务指针原子切换到它。

    ``version`` 必须是事先用 :func:`next_practice_version` 取到的版本号
    （与落盘文件名中的版本一致）；UNIQUE(task_id, version) 在并发冲突时
    直接抛错，绝不复用或覆盖既有版本。
    必须在 MusicXML 已原子落盘**之后**调用。重建失败时不调用本函数，
    因此旧版本与指针都不会被改写。
    """
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO practice_score_versions
               (task_id, version, status, source_musicxml_sha256,
                alignment_version, manifest_json, musicxml_path,
                musicxml_sha256)
               VALUES (?,?, 'built', ?, ?, ?, ?, ?)""",
            (task_id, version, source_musicxml_sha256, alignment_version,
             manifest_json, musicxml_path, musicxml_sha256))
        conn.execute(
            """UPDATE practice_scores
               SET status='built', current_version=?,
                   built_at=datetime('now')
             WHERE id=?""",
            (version, task_id))


def delete_practice_score(task_id: str):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM practice_scores WHERE id=?",
                           (task_id,))
        return cur.rowcount


# ------------------------------------------------------------ 谱面修订迁移

def create_revision_piece(piece_id: str, title: str, musicxml_path: str,
                          revision_of: str, revision_id: str):
    """创建谱面修订后的新复盘入口（尚无音频，status=created）。"""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO pieces
               (id, title, musicxml_path, status, warnings_json,
                revision_of, revision_id)
               VALUES (?,?,?, 'created', '{"warnings": [], "peaks": []}', ?, ?)""",
            (piece_id, title, musicxml_path, revision_of, revision_id))


def save_revision_score_notes(piece_id: str, score_notes, warnings=None):
    """写入修订入口的展开谱音（无音频）；失败状态由音频上传后的分析接管。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM score_notes WHERE piece_id=?", (piece_id,))
        conn.executemany(
            """INSERT INTO score_notes
               (piece_id, idx, midi, beat, dur_q, measure,
                beat_in_measure, occurrence, part)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(piece_id, n["index"], n["midi"], n["beat"], n["dur_q"],
              n.get("measure"), n.get("beat_in_measure"),
              n.get("occurrence"), n.get("part", 0)) for n in score_notes])
        conn.execute(
            "UPDATE pieces SET warnings_json=?, status='created' WHERE id=?",
            (json.dumps({"warnings": warnings or [], "peaks": []},
                        ensure_ascii=False), piece_id))


def create_score_revision(revision_id: str, source_piece_id: str, title: str,
                          source_hash: str, revised_hash: str,
                          source_alignment_version, revised_path: str,
                          has_blockers: bool, blockers_json: str,
                          mapping_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO score_revisions
               (id, source_piece_id, title, status,
                source_musicxml_sha256, revised_musicxml_sha256,
                source_alignment_version, revised_path, has_blockers,
                blockers_json, mapping_json)
               VALUES (?,?,?, 'preview',?,?,?,?,?,?,?)""",
            (revision_id, source_piece_id, title, source_hash, revised_hash,
             source_alignment_version, revised_path,
             1 if has_blockers else 0, blockers_json, mapping_json))


def get_score_revision(revision_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM score_revisions WHERE id=?",
                           (revision_id,)).fetchone()
        return dict(row) if row else None


def list_score_revisions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM score_revisions ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def commit_score_revision(revision_id: str, new_piece_id: str,
                          inherited_json: str, carryover_json: str):
    """确认提交：绑定新复盘入口，固化继承锚点与接续模板。

    一次性原子更新；提交后 mapping/blockers 与来源哈希全部只读，不再改写。
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE score_revisions
               SET status='committed', new_piece_id=?, inherited_json=?,
                   carryover_templates_json=?, committed_at=datetime('now')
             WHERE id=?""",
            (new_piece_id, inherited_json, carryover_json, revision_id))


def delete_score_revision(revision_id: str):
    """只允许删除 preview；committed 记录为只读历史，返回 0 行。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM score_revisions WHERE id=? AND status='preview'",
            (revision_id,))
        return cur.rowcount


def get_revision_idempotent(request_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM revision_idempotency WHERE request_id=?",
            (request_id,)).fetchone()
        return dict(row) if row else None


def save_revision_idempotent(request_id: str, revision_id, status_code: int,
                             response_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO revision_idempotency
               (request_id, revision_id, status_code, response_json)
               VALUES (?,?,?,?)""",
            (request_id, revision_id, status_code, response_json))


def add_anchor_direct(piece_id: str, kind: str, score_index, note=""):
    """谱面修订迁移：把可继承的 exclude_score 锚点预置到新复盘入口。

    与普通 add_anchor 区分：不带 obs_index 且不触发重算（此时尚无音频）。
    """
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO anchors
               (piece_id, kind, score_index, obs_index, note)
               VALUES (?,?,?,NULL,?)""",
            (piece_id, kind, score_index, note))
