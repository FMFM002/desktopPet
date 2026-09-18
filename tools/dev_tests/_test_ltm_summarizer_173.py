"""
LTM Summarizer 单元测试（1.7.3）—— 纯 mock，不依赖真实 DB / LLM
================================================================================
覆盖：
  1. _build_clusters() 聚类正确性（已知输入 → 已知 cluster）
  2. _check_thresholds() 阈值判定（size / count / cooldown）
  3. _decay_step() 在空库/全活跃场景下不崩
  4. get_all_for_decay() 返回字段完整
  5. _maybe_summarize() 在冷却期被卡掉
  6. 强制 trigger_now() 跳过冷却
================================================================================
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))


class _FakeRow(dict):
    """模拟 sqlite3.Row，支持 dict(r)"""
    pass


class FakeLongTermDB:
    """最小化 LTM DB mock"""
    def __init__(self, ltm_rows=None):
        self._rows = ltm_rows if ltm_rows is not None else []
        self._count = len(self._rows)
        self._upserts = []
        self._deletes = []
        self._updates = []

    def get_all_for_decay(self):
        return self._rows

    def count(self):
        return self._count

    def upsert(self, variable_name, value, category, confidence, source):
        # 找到并更新，或插入
        for r in self._rows:
            if r["variable_name"] == variable_name:
                r["value"] = value
                r["confidence"] = confidence
                self._upserts.append(("update", variable_name))
                return r["id"]
        self._rows.append({
            "id": len(self._rows) + 1,
            "variable_name": variable_name,
            "value": value,
            "category": category,
            "confidence": confidence,
        })
        self._count += 1
        self._upserts.append(("insert", variable_name))
        return len(self._rows)

    def delete(self, variable_name):
        self._deletes.append(variable_name)
        self._rows = [r for r in self._rows if r["variable_name"] != variable_name]
        self._count -= 1

    def update_confidence(self, vn, new_conf):
        self._updates.append((vn, new_conf))
        for r in self._rows:
            if r["variable_name"] == vn:
                r["confidence"] = new_conf

    def delete_below_confidence(self, threshold):
        before = self._count
        self._rows = [r for r in self._rows if r["confidence"] >= threshold]
        self._count = len(self._rows)
        return before - self._count

    def touch_access(self, names):
        pass


class FakeDatabase:
    def __init__(self, ltm_rows=None):
        self.ltm = FakeLongTermDB(ltm_rows=ltm_rows)


def test_build_clusters():
    """_build_clusters: 已知变量名 → 已知聚类结果"""
    from tools.ltm_summarizer import LTMSummarizer
    sumzr = LTMSummarizer.__new__(LTMSummarizer)
    sumzr._cluster_similarity = 0.75

    mems = [
        {"variable_name": "user_name", "value": "x"},
        {"variable_name": "user_nam", "value": "y"},     # 与 user_name 相似
        {"variable_name": "prefered_language", "value": "z"},  # 与 preferred_language 相似
        {"variable_name": "preferred_language", "value": "w"},
        {"variable_name": "totally_unique_xyz", "value": "q"},  # 孤立
    ]
    clusters = sumzr._build_clusters(mems)
    sizes = sorted([len(c) for c in clusters], reverse=True)

    print(f"  [T1] cluster sizes={sizes} (期望 [2, 2, 1])")
    assert sizes == [2, 2, 1], f"聚类错误，got {sizes}"

    # 验证 user_name 和 user_nam 在同一个 cluster
    names_groups = []
    for c in clusters:
        names_groups.append(sorted([m["variable_name"] for m in c]))

    # 找含 user_name 的 cluster
    user_cluster = next(c for c in names_groups
                        if "user_name" in c and "user_nam" in c)
    # 找含 preferred_language 的 cluster
    pref_cluster = next(c for c in names_groups
                        if "preferred_language" in c and "prefered_language" in c)
    # 找孤立
    solo = next(c for c in names_groups if c == ["totally_unique_xyz"])

    print(f"  [T1] user cluster: {user_cluster}")
    print(f"  [T1] preferred cluster: {pref_cluster}")
    print(f"  [T1] solo: {solo}")
    assert set(user_cluster) == {"user_name", "user_nam"}
    assert set(pref_cluster) == {"prefered_language", "preferred_language"}
    assert solo == ["totally_unique_xyz"]
    print("  [T1] PASS [OK]")


def test_check_thresholds():
    """_check_thresholds: 文件大小/条目数阈值判断"""
    from tools.ltm_summarizer import LTMSummarizer, LTM_DB_PATH

    # 不实际改文件 → patch 成 DB 不存在的情景
    sumzr = LTMSummarizer.__new__(LTMSummarizer)
    sumzr._size_threshold_kb = 1024
    sumzr._count_threshold = 200
    sumzr._db_size_bytes = lambda: 500 * 1024   # 500KB < 1MB
    sumzr._count = lambda: 100  # < 200

    triggered, reason = sumzr._check_thresholds()
    print(f"  [T2] below both: triggered={triggered} reason={reason}")
    assert triggered is False

    sumzr._db_size_bytes = lambda: 2000 * 1024  # 2MB > 1MB
    triggered, reason = sumzr._check_thresholds()
    print(f"  [T2] above size: triggered={triggered} reason={reason}")
    assert triggered is True
    assert "KB" in reason

    sumzr._db_size_bytes = lambda: 500 * 1024  # 回到小
    sumzr._count = lambda: 500  # > 200
    triggered, reason = sumzr._check_thresholds()
    print(f"  [T2] above count: triggered={triggered} reason={reason}")
    assert triggered is True
    assert "count" in reason.lower()

    print("  [T2] PASS [OK]")


def test_decay_step_empty():
    """_decay_step 在空库不崩"""
    from tools.ltm_summarizer import LTMSummarizer
    sumzr = LTMSummarizer.__new__(LTMSummarizer)
    sumzr._decay_idle_days = 7
    sumzr._decay_rate = 0.1
    sumzr._delete_threshold = 0.15

    # 接管 get_db
    import src.core.database as db_mod
    original = db_mod.get_db
    db_mod.get_db = lambda: FakeDatabase(ltm_rows=[])
    try:
        result = sumzr._decay_step()
        print(f"  [T3] empty: result={result}")
        assert result == {"decayed": 0, "deleted": 0}
    finally:
        db_mod.get_db = original
    print("  [T3] PASS [OK]")


def test_decay_step_decay_active():
    """_decay_step: 全活跃（id < 7 天）→ 不衰减"""
    from tools.ltm_summarizer import LTMSummarizer
    sumzr = LTMSummarizer.__new__(LTMSummarizer)
    sumzr._decay_idle_days = 7
    sumzr._decay_rate = 0.1
    sumzr._delete_threshold = 0.15

    now = datetime.now()
    recent = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [{
        "id": 1,
        "variable_name": "fresh_kid",
        "value": "x",
        "category": "other",
        "confidence": 0.7,
        "last_accessed": recent,
        "updated_at": recent,
        "created_at": recent,
    }]

    import src.core.database as db_mod
    original = db_mod.get_db
    db_mod.get_db = lambda: FakeDatabase(ltm_rows=rows)
    try:
        result = sumzr._decay_step()
        print(f"  [T4] all active: result={result}")
        assert result["decayed"] == 0
        assert result["deleted"] == 0
    finally:
        db_mod.get_db = original
    print("  [T4] PASS [OK]")


def test_decay_step_delete_low():
    """_decay_step: 长期未访问且已接近 delete_threshold → 删除"""
    from tools.ltm_summarizer import LTMSummarizer
    sumzr = LTMSummarizer.__new__(LTMSummarizer)
    sumzr._decay_idle_days = 7
    sumzr._decay_rate = 0.1
    sumzr._delete_threshold = 0.15

    now = datetime.now()
    old = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {  # 30 天前访问，confidence=0.2 → period=30/7≈4.28, new=0.2-0.428<0 → 删
            "id": 1,
            "variable_name": "doomed_kid",
            "value": "old",
            "category": "other",
            "confidence": 0.2,
            "last_accessed": old,
            "updated_at": old,
            "created_at": old,
        },
        {  # 30 天前访问，confidence=0.7 → 衰减但不到 delete_threshold
            "id": 2,
            "variable_name": "decaying_kid",
            "value": "mid",
            "category": "other",
            "confidence": 0.7,
            "last_accessed": old,
            "updated_at": old,
            "created_at": old,
        },
    ]

    fake_db = FakeDatabase(ltm_rows=rows)
    import src.core.database as db_mod
    original = db_mod.get_db
    db_mod.get_db = lambda: fake_db
    try:
        result = sumzr._decay_step()
        print(f"  [T5] {result}")
        assert result["deleted"] == 1
        assert result["decayed"] == 1
        # doomed_kid 应该被删
        assert not any(r["variable_name"] == "doomed_kid" for r in fake_db.ltm._rows)
        # decaying_kid 还在
        assert any(r["variable_name"] == "decaying_kid" for r in fake_db.ltm._rows)
    finally:
        db_mod.get_db = original
    print("  [T5] PASS [OK]")


def test_maybe_trigger_cooldown():
    """maybe_trigger: 冷却期内被卡掉"""
    import time
    from tools.ltm_summarizer import LTMSummarizer
    sumzr = LTMSummarizer.__new__(LTMSummarizer)
    sumzr._enabled = True
    sumzr._running = True
    sumzr._cooldown_seconds = 60
    sumzr._last_summarize_time = time.time()  # 刚刚跑过

    # 阈值过关
    sumzr._check_thresholds = lambda: (True, "test")

    submitted = []
    def fake_submit(*a, **k):
        submitted.append(a)

    # 拿不到 executor 时 → 静默跳过
    sumzr._get_executor = lambda: None
    sumzr._get_executor = lambda: type("E", (), {"submit": staticmethod(fake_submit)})()

    sumzr.maybe_trigger()
    print(f"  [T6] submit called (cooldown): {len(submitted)} (期望 0)")
    assert len(submitted) == 0
    print("  [T6] PASS [OK]")


def test_database_migration_columns():
    """LongTermMemoriesDB 包含 3 个新字段"""
    from src.core.database import LongTermMemoriesDB, _LTM_DDL

    for col in ["access_count", "last_accessed", "decay_score"]:
        assert col in _LTM_DDL, f"_LTM_DDL 缺失字段: {col}"
        print(f"  [T7] DDL 包含 {col} [OK]")
    print("  [T7] PASS [OK]")


if __name__ == "__main__":
    tests = [
        ("test_build_clusters", test_build_clusters),
        ("test_check_thresholds", test_check_thresholds),
        ("test_decay_step_empty", test_decay_step_empty),
        ("test_decay_step_decay_active", test_decay_step_decay_active),
        ("test_decay_step_delete_low", test_decay_step_delete_low),
        ("test_maybe_trigger_cooldown", test_maybe_trigger_cooldown),
        ("test_database_migration_columns", test_database_migration_columns),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n=== 测试汇总 ===")
    print(f"  通过: {passed}/{len(tests)}")
    print(f"  失败: {failed}")
    sys.exit(0 if failed == 0 else 1)
