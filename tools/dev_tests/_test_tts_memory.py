"""验证 GENIE worker 空闲释放逻辑（不真的加载模型）"""
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

ok = True
def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


print("== 配置默认值 ==")
from src.utils.config import TTSConfig
t = TTSConfig()
check("默认不启动预加载（省 4.5GB）", t.genie_preload_on_start is False)
check("默认空闲 15 分钟释放", t.genie_idle_release_minutes == 15)

print("== 空闲释放逻辑 ==")
from src.core.tts_engine import TTSEngine


class FakeProc:
    def __init__(self):
        self.alive = True
    def is_alive(self):
        return self.alive
    def join(self, timeout=None):
        pass
    def terminate(self):
        self.alive = False


eng = TTSEngine()
eng._provider = "genie"
eng._genie_ready = True
eng._genie_loading = False
eng._genie_busy = False
eng._genie_worker = FakeProc()

# 未到时间 → 不释放
eng._genie_last_used = time.time()
eng._config_minutes = None
stopped = []
eng._stop_genie_worker = lambda: stopped.append(1)
eng._maybe_release_idle_genie()
check("刚用过 → 不释放", not stopped)

# 设为 1 分钟并把最后使用时间推早 → 应释放
import src.utils.config as cfgmod
_cfg = cfgmod.get_config()
_cfg.tts.genie_idle_release_minutes = 1
eng._genie_last_used = time.time() - 120
eng._maybe_release_idle_genie()
check("空闲超时 → 释放 worker", bool(stopped))
check("释放后 ready 置 False", eng._genie_ready is False)
check("标记为空闲释放", eng._genie_idle_released is True)

# 0 = 从不释放
stopped.clear()
eng._genie_ready = True
eng._genie_worker = FakeProc()
_cfg.tts.genie_idle_release_minutes = 0
eng._genie_last_used = time.time() - 99999
eng._maybe_release_idle_genie()
check("配置 0 → 从不释放", not stopped)

# 忙时不释放
stopped.clear()
eng._genie_busy = True
_cfg.tts.genie_idle_release_minutes = 1
eng._maybe_release_idle_genie()
check("正在合成 → 不释放", not stopped)

print("== 启动预加载开关 ==")
_cfg.tts.genie_preload_on_start = True
check("配置可写回", cfgmod.get_config().tts.genie_preload_on_start is True)

print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
