"""
[DB-SCHEMA] F-R4-3 配套护栏: backend 全部 ORM 声明列 ⊆ SQLite 实际表列
运行: python qa/harness/run_db_schema.py
产物: qa/round-2/artifacts/result_DB-SCHEMA.json

为什么需要它 (verify-round4 §9 F-R4-3):
  SQLAlchemy `Base.metadata.create_all()` 只补 **缺表**, 对已存在的表 **不补新列**。
  于是"给模型加一列 + 老库文件(data/peiyinyuan.db)"这个组合会静默产出缺列的表,
  直到某次读写才炸 `no such column`。既有 harness 里没有任何一条断言看得到这件事
  (P1/P2/P4/P6 跑通只说明"当前库里恰好有这些列")。

四个用例:
  1) 现库对账      : 生产库(只读备份)逐表断言 声明列 ⊆ 实际列, 且 ORM 表全部存在。
  2) 建表清单充分性 : base._ensure_all_tables()/init_db() 里那份 import 清单是否覆盖
                     所有定义了模型的模块 (漏一个 => 全新库缺一张表)。
  3) 缺列旧库模拟   : 在临时副本上真的 DROP 一列(SQLite 3.35+ 支持), 先证明运行期
                     确实报 no such column, 再走生产迁移函数, 断言"自动补齐 + 幂等 +
                     无附带损伤"。F-R4-3 未落地时这里必须记 FAIL —— 它就是回炉追踪器。
  4) 缺表旧库模拟   : 副本上 DROP 整张表, 走 create_all+迁移, 断言表与列都被补回。

纪律: 全程不写生产库 (mode=ro 读取 + 只在临时副本上 ALTER); 不使用 mock;
      环境不可用(库被独占/备份失败)记 BLOCKED(ENV), 逻辑断言照常执行。
"""
import importlib
import inspect
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

# 产物落到 round-2/artifacts (与 run_e1_engine_fail.py 同一手法), 不覆盖 round-1 基线
H.ARTIFACTS = H.QA_DIR / "round-2" / "artifacts"
(H.ARTIFACTS).mkdir(parents=True, exist_ok=True)
H.LOGS = H.QA_DIR / "round-2" / "logs"
H.LOGS.mkdir(parents=True, exist_ok=True)

MODELS_DIR = H.PROJECT_ROOT / "backend" / "models"
# 用例3 的模拟对象: 普通可空、无索引、无约束引用的列 —— 模拟"模型新加了一列, 老库没有"
SIM_TABLE = "conversion_tasks"
SIM_COLUMN = "error_message"
SIM_TABLE_FK = "conversion_tasks"      # 用例3 里顺带读的其它列, 用于附带损伤对账
SIM_PROBE_SQL_AFTER = f'SELECT {SIM_COLUMN} FROM "{SIM_TABLE}" LIMIT 1'


def declared_schema():
    """真值集: 导入 backend/models 下全部模型模块, 取 Base.metadata 的 表 -> 列集合。

    返回 (tables, modules_by_table): modules_by_table 记录每张表由哪个模块声明,
    供用例2 判断"建表清单是否覆盖到它"。
    """
    from backend.models.base import Base
    tables, modules = {}, {}
    for p in sorted(MODELS_DIR.glob("*.py")):
        if p.stem.startswith("_") or p.stem == "base":
            continue
        mod = importlib.import_module(f"backend.models.{p.stem}")
        src = p.read_text(encoding="utf-8")
        found = re.findall(r"class\s+(\w+)\(\s*Base\s*\)", src)
        for cls in found:
            klass = getattr(mod, cls, None)
            if klass is None or not hasattr(klass, "__table__"):
                continue
            t = klass.__table__
            tables[t.name] = {c.name for c in t.columns}
            modules[t.name] = p.stem
    return tables, modules


def actual_schema(db_path):
    """实际集: PRAGMA table_info 逐表读列。"""
    out = {}
    conn = sqlite3.connect(str(db_path))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for n in names:
            out[n] = {r[1] for r in conn.execute(f'PRAGMA table_info("{n}")')}
    finally:
        conn.close()
    return out


def row_counts(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def copy_live_db(dst, notes):
    """只读方式取一份生产库副本。优先 sqlite backup API(能正确处理 WAL),
    失败退回文件复制(把 -wal/-shm 一并带上)。两条路径都记进 notes —— 归因要说得清。
    """
    src = H.DB_PATH
    if not src.exists():
        return None
    try:
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        try:
            d = sqlite3.connect(str(dst))
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        notes.append("副本来源=sqlite3 backup API (mode=ro, 不写生产库)")
        return dst
    except Exception as e:
        notes.append(f"backup API 失败({type(e).__name__}: {e}) -> 退回文件复制")
    try:
        shutil.copy2(src, dst)
        for suf in ("-wal", "-shm"):
            if Path(str(src) + suf).exists():
                shutil.copy2(str(src) + suf, str(dst) + suf)
        notes.append("副本来源=文件复制(含 -wal/-shm); 若生产库正被写入, 副本可能非一致快照")
        return dst
    except Exception as e:
        notes.append(f"文件复制也失败({type(e).__name__}: {e})")
        return None


def bootstrap_imports(fn):
    """从生产函数源码里抽出 `from backend.models.X import ...` 清单。

    为什么看源码而不是看 metadata: _ensure_all_tables 的 *契约* 就是"确保所有模型表
    都被创建"(docstring 原文), 契约写在它自己的 import 清单里。清单少一个模块 =>
    只走这条路径建库的全新安装会缺表。
    """
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    return set(re.findall(r"from\s+backend\.models\.(\w+)\s+import", src))


def drop_column(db_path, table, col):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
        conn.commit()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def drop_table(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.commit()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def probe_select(db_path, sql):
    """真读一次: 缺列时 SQLite 会报 OperationalError —— 这是用户可见的故障面。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql).fetchall()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def production_migrate(engine, declared, notes):
    """调用生产侧列迁移 (F-R4-3)。返回 (存在?, 补齐的 "表.列" 列表)。"""
    from backend.models import base
    fn = getattr(base, "_migrate_added_columns", None)
    if fn is None:
        notes.append("生产码里没有 _migrate_added_columns: create_all 之外无任何列迁移")
        return False, []
    added = fn(engine)
    return True, list(added or [])


def main():
    log = H.LOGS / "db_schema.log"
    work = H.TMP / f"db_schema_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    notes = []
    with H.Tee(log):
        su = H.Suite("DB-SCHEMA", "run_db_schema.py")
        su.set_meta("purpose", "F-R4-3 配套护栏: ORM 声明列 ⊆ SQLite 实际表列")
        su.set_meta("sqlite_lib_version", sqlite3.sqlite_version)
        su.set_meta("live_db", str(H.DB_PATH))
        su.set_meta("path_substitution",
                    "用例3/4 在临时副本上执行 create_all + base._migrate_added_columns, "
                    "即生产启动路径的建表/补列两步; 未跑 _init_builtin_voices(它会写生产库, "
                    "与列一致性无关)。该替代由用例2 的源码引用断言兜底(迁移确实挂在启动路径上)。")

        declared, modules_by_table = declared_schema()
        su.set_meta("orm_tables", sorted(declared))
        su.set_meta("orm_column_total", sum(len(v) for v in declared.values()))
        H.kv("ORM 声明", f"{len(declared)} 表 / {sum(len(v) for v in declared.values())} 列 "
                        f"(逐表读自 backend/models/*.py)")

        # ---------- 用例1: 现库对账 ----------
        r1 = su.new(f"现库对账: ORM 声明列 ⊆ SQLite 实际列 ({H.DB_PATH.name})")
        r1.evidence_add(log)
        live_copy = work / "live_copy.db"
        got = copy_live_db(live_copy, notes)
        for n in notes:
            H.kv("  归因", n)
        if got is None:
            r1.block("读取生产库副本", "ENV: 备份 API 与文件复制都失败(库被独占或文件缺失), "
                                       "本用例不可观测, 不判 PASS/FAIL")
            r1.notes.append("用例3/4 改在 create_all 建出的临时库上模拟, 与生产库无关。")
        else:
            act = actual_schema(live_copy)
            missing_tables = sorted(set(declared) - set(act))
            missing_cols = {t: sorted(declared[t] - act[t]) for t in declared if t in act}
            missing_cols = {t: v for t, v in missing_cols.items() if v}
            extra_cols = {t: sorted(act[t] - declared[t]) for t in act
                          if t in declared and act[t] - declared[t]}
            su.set_meta("live_tables", sorted(act))
            su.set_meta("live_missing_columns", missing_cols)
            su.set_meta("live_extra_columns", extra_cols)
            for t in sorted(set(declared) | set(act)):
                H.kv(f"  {t}", f"声明 {len(declared.get(t, set()))} / 实际 {len(act.get(t, set()))}"
                               f"{'  缺列: ' + str(missing_cols.get(t)) if missing_cols.get(t) else ''}")
            r1.check("生产库中 ORM 声明的表全部存在", not missing_tables,
                     f"缺表={missing_tables}" if missing_tables
                     else f"{len(declared)}/{len(declared)} 表在场")
            r1.check("生产库逐表满足 声明列 ⊆ 实际列 (无静默缺列)", not missing_cols,
                     f"缺列={missing_cols}" if missing_cols else
                     f"{sum(len(v) for v in declared.values())} 个声明列全部在场")
            r1.check("护栏有鉴别力: 对账确实读到了列级明细(非恒真)",
                     all(len(act.get(t, set())) > 0 for t in declared),
                     f"逐表列数={[len(act.get(t, set())) for t in sorted(declared)]}")
            if extra_cols:
                r1.notes.append(f"实际库比声明多出的列(历史遗留/手工加列, 不判 FAIL): {extra_cols}")

        # ---------- 用例2: 建表清单充分性 ----------
        r2 = su.new("建表清单充分性: _ensure_all_tables/init_db 的 import 清单覆盖全部模型模块")
        r2.evidence_add(log)
        from backend.models import base as base_mod
        boot_ensure = bootstrap_imports(base_mod._ensure_all_tables)
        boot_init = bootstrap_imports(getattr(base_mod, "init_db", None))
        need = set(modules_by_table.values())
        su.set_meta("bootstrap_imports_ensure_all_tables", sorted(boot_ensure or []))
        su.set_meta("bootstrap_imports_init_db", sorted(boot_init or []))
        H.kv("清单(_ensure_all_tables)", sorted(boot_ensure or []))
        H.kv("清单(init_db)", sorted(boot_init or []))
        H.kv("声明模型的模块", sorted(need))
        miss = sorted(need - (boot_ensure or set()))
        r2.check("_ensure_all_tables 的建表清单覆盖所有声明模型的模块", not miss,
                 f"清单={sorted(boot_ensure or [])} 未纳入={miss} -> 只走这条路径建库的全新安装会缺表 "
                 f"(涉及表={[t for t, m in modules_by_table.items() if m in miss]})"
                 if miss else f"{len(need)} 个模型模块全在清单内")
        miss_init = sorted(need - (boot_init or set()))
        r2.check("init_db 的建表清单同样覆盖所有模型模块", not miss_init,
                 f"未纳入={miss_init}" if miss_init else "OK")
        # 契约兜底: 迁移函数必须真的挂在启动路径上, 否则"存在但没人调"= 没有
        src_ensure = inspect.getsource(base_mod._ensure_all_tables)
        src_init = inspect.getsource(base_mod.init_db)
        hooked = "_migrate_added_columns" in src_ensure or "_migrate_added_columns" in src_init
        r2.check("列迁移挂在启动路径上(被 _ensure_all_tables 或 init_db 调用)", hooked,
                f"_ensure_all_tables 引用={ '_migrate_added_columns' in src_ensure } "
                f"init_db 引用={ '_migrate_added_columns' in src_init }")

        # 行为证明(而不是源码对账): 全新库只走那份清单, 到底建出了哪些表。
        # 必须开子进程 —— 本进程已 import 过全部模型, Base.metadata 被污染, 再 create_all
        # 一定"全都有", 那就是恒真。
        probe = work / "fresh_bootstrap_probe.py"
        probe.write_text(
            "import sys, re, inspect, json, importlib\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, r'%s')\n"
            "from sqlalchemy import create_engine\n"
            "from backend.models import base\n"
            "src = inspect.getsource(base._ensure_all_tables)\n"
            "mods = re.findall(r'from\\s+backend\\.models\\.(\\w+)\\s+import', src)\n"
            "for m in sorted(set(mods)):\n"
            "    importlib.import_module('backend.models.' + m)\n"
            "eng = create_engine('sqlite:///' + sys.argv[1])\n"
            "base.Base.metadata.create_all(bind=eng)\n"
            "print(json.dumps({'imported': sorted(set(mods)),\n"
            "                  'tables': sorted(base.Base.metadata.tables.keys())}))\n"
            % str(H.PROJECT_ROOT), encoding="utf-8")
        fresh_db = work / "fresh_via_bootstrap.db"
        import json as _json
        import subprocess
        proc = subprocess.run([sys.executable, str(probe), str(fresh_db)],
                              capture_output=True, text=True, encoding="utf-8",
                              env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
                              timeout=180)
        fresh = {}
        try:
            fresh = _json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            fresh = {}
        su.set_meta("fresh_install_bootstrap", fresh or {"stderr": proc.stderr[-300:]})
        if not fresh:
            r2.block("全新库实际建表对账(子进程)", f"ENV: 探针子进程 rc={proc.returncode} "
                                                  f"err={proc.stderr[-200:]}")
        else:
            H.kv("子进程只 import 清单", fresh["imported"])
            H.kv("全新库建出的表", fresh["tables"])
            fresh_missing = sorted(set(declared) - set(fresh["tables"]))
            r2.check("全新安装只走生产建表清单也能得到全部 ORM 表(行为证明)", not fresh_missing,
                     f"建出的表={fresh['tables']} 建不出的={fresh_missing} "
                     f"-> 该库里这些表连表都没有, 相关功能在老库/新装库上必炸"
                     if fresh_missing else f"{len(declared)} 表全建出")
            r2.check("探针与静态清单一致(证明探针复现的是生产清单, 不是本进程被污染的 metadata)",
                     set(fresh["imported"]) == set(boot_ensure or set()),
                     f"子进程 import={sorted(fresh['imported'])} 静态抽取={sorted(boot_ensure or [])}")
        r2.notes.append("F-R4-3 现状: backend/models/__init__.py 为空, 模型注册靠各调用点 import; "
                        "生产 app 因 register_routes() 先 import 了 backend.api.memory -> "
                        "backend.services.memory_service -> models.memory, 所以现库里 memories 表在场; "
                        "但脱离 app 直接 init_db() 的路径不会, 故按清单契约判 FAIL/修复。")

        # ---------- 用例3: 缺列旧库模拟 (回炉追踪器) ----------
        r3 = su.new(f"缺列旧库模拟: 副本 DROP {SIM_TABLE}.{SIM_COLUMN} -> 生产迁移是否自动补齐 (F-R4-3)")
        r3.evidence_add(log)
        legacy = work / "legacy_missing_column.db"
        base_db = live_copy if live_copy and live_copy.exists() else None
        if base_db:
            shutil.copy2(base_db, legacy)
        else:
            # 生产库不可读时, 用 create_all 现场建一张等价的"旧库" (记 DEGRADED 归因)
            e0 = create_engine(f"sqlite:///{legacy.as_posix()}")
            from backend.models.base import Base
            Base.metadata.create_all(bind=e0)
            e0.dispose()
            r3.notes.append("ENV: 生产库副本不可用 -> 旧库基底改用 create_all 现场建表 "
                            "(列一致性断言仍成立, 但数据行不是生产快照)")
        before = actual_schema(legacy)
        ok, err = drop_column(legacy, SIM_TABLE, SIM_COLUMN)
        su.set_meta("simulate_drop", {"table": SIM_TABLE, "column": SIM_COLUMN,
                                      "ok": ok, "err": err})
        if not ok:
            r3.block("模拟缺列旧库", f"ENV: ALTER TABLE DROP COLUMN 失败 ({err}), "
                                     f"SQLite {sqlite3.sqlite_version} 需 >= 3.35")
        else:
            after_drop = actual_schema(legacy)
            # 自我鉴别: 模拟必须真的把列弄丢, 否则整条用例是恒真
            r3.check(f"模拟成立: 副本上 {SIM_TABLE} 确实少了 {SIM_COLUMN} 列",
                     SIM_COLUMN not in after_drop.get(SIM_TABLE, set())
                     and SIM_COLUMN in before.get(SIM_TABLE, set()),
                     f"drop 前列数={len(before.get(SIM_TABLE, set()))} "
                     f"drop 后={len(after_drop.get(SIM_TABLE, set()))}")
            readable, err = probe_select(legacy, SIM_PROBE_SQL_AFTER)
            r3.check("故障面真实: 缺列后按列读取报 no such column (用户可见故障)",
                     not readable and "no such column" in err,
                     f"SELECT -> {err[:120]}")
            counts_before = row_counts(legacy)
            engine = create_engine(f"sqlite:///{legacy.as_posix()}")
            exists, added = production_migrate(engine, declared, notes)
            su.set_meta("migration_returns", added)
            r3.check("F-R4-3 落地: 启动路径存在列迁移函数 _migrate_added_columns", exists,
                     "backend/models/base.py 未提供 -> 记 FAIL 现状, 作为回炉追踪器"
                     if not exists else "存在且被 _ensure_all_tables 调用(用例2 已验)")
            if exists:
                tgt = f"{SIM_TABLE}.{SIM_COLUMN}"
                r3.check(f"迁移自动补齐被删的列 ({tgt} 出现在返回值里)", tgt in added,
                         f"补齐列表={added}")
                healed = actual_schema(legacy)
                ok2, err2 = probe_select(legacy, SIM_PROBE_SQL_AFTER)
                r3.check("补齐后按列读取恢复 (旧库自愈)", ok2, err2[:120])
                still_missing = {t: sorted(declared[t] - healed.get(t, set()))
                                 for t in declared if healed.get(t)}
                still_missing = {t: v for t, v in still_missing.items() if v}
                r3.check("补齐后整库满足 声明列 ⊆ 实际列",
                         not still_missing,
                         "全部声明列在场" if not still_missing else f"仍缺={still_missing}")
                counts_after = row_counts(legacy)
                counts_diff = {k: (counts_before.get(k), counts_after.get(k))
                               for k in set(counts_before) | set(counts_after)
                               if counts_before.get(k) != counts_after.get(k)}
                r3.check("无附带损伤: 行数与表集合逐表不变 (只 ADD, 不删改搬)",
                         not counts_diff,
                         f"行数差异={counts_diff}" if counts_diff
                         else f"{len(counts_before)} 张表行数逐一相等")
                again = production_migrate(engine, declared, notes)[1]
                r3.check("幂等: 二次调用不再 ADD 任何列", again == [], f"二次返回={again}")
            else:
                r3.fail("缺列旧库能被自动补齐 (F-R4-3 未落地 -> 本行即追踪器)",
                        f"生产码无列迁移; 副本仍缺 {SIM_TABLE}.{SIM_COLUMN}; "
                        f"一旦模型加列, 老库文件会在运行期报 no such column")
            engine.dispose()

        # ---------- 用例4: 缺表旧库模拟 (create_all 的本职) ----------
        r4 = su.new("缺表旧库模拟: 副本 DROP memories -> 生产启动路径应把整表补回")
        r4.evidence_add(log)
        tab = "memories"
        if tab not in declared:
            r4.block("模拟缺表旧库", "ORM 里已无 memories 表, 用例需换目标表")
        else:
            legacy2 = work / "legacy_missing_table.db"
            if live_copy and live_copy.exists():
                shutil.copy2(live_copy, legacy2)
            else:
                e0 = create_engine(f"sqlite:///{legacy2.as_posix()}")
                from backend.models.base import Base
                Base.metadata.create_all(bind=e0)
                e0.dispose()
            ok, err = drop_table(legacy2, tab)
            su.set_meta("simulate_drop_table", {"table": tab, "ok": ok, "err": err})
            if not ok:
                r4.block("DROP 目标表", f"ENV: {err}")
            else:
                gone = actual_schema(legacy2)
                r4.check(f"模拟成立: 副本上 {tab} 表已消失", tab not in gone,
                         f"剩余表={sorted(gone)}")
                e2 = create_engine(f"sqlite:///{legacy2.as_posix()}")
                from backend.models.base import Base
                Base.metadata.create_all(bind=e2)
                production_migrate(e2, declared, notes)
                back = actual_schema(legacy2)
                r4.check(f"{tab} 表被自动补回且列与声明一致",
                         tab in back and declared[tab] <= back.get(tab, set()),
                         f"补回列数={len(back.get(tab, set()))}/{len(declared[tab])} "
                         f"缺={sorted(declared[tab] - back.get(tab, set()))}")
                e2.dispose()

        su.set_meta("copy_path_notes", notes)
        su.finish(exit_when_fail=False)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
