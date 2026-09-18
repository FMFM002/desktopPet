"""验证设置页「工具管理」排版：开启 + 加载策略都在左侧，介绍独立成列"""
import os, sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

from PySide6.QtWidgets import QApplication, QComboBox, QCheckBox, QLabel, QPushButton
app = QApplication(sys.argv)

ok = True
def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


from src.ui.settings_window import SettingsWindow
sw = SettingsWindow()
sw.show()
for _ in range(30):
    app.processEvents()

page = sw._category_pages.get("tools")
check("工具设置页存在", page is not None)

grid = getattr(sw, "_tools_grid", None)
check("使用 QGridLayout 排版", grid is not None)

n_rows = grid.rowCount() if grid else 0
# ★ 1.9.0: 现在按「加载策略下拉」计数（旧的开启复选框已被"关闭"档取代），
#   列表里还多了自动工具 idle_talk
n_tools = len(sw._tools_policy_combos)
check("每个工具一行（+表头）", n_rows == n_tools + 1, f"rows={n_rows} tools={n_tools}")
check("主动说话也排在列表里（idle_talk 自动工具）", "idle_talk" in sw._tools_policy_combos)

# 每行：col0=加载策略下拉, col1=工具名, col2=设置按钮, col3=介绍按钮
bad = []
for name, combo in sw._tools_policy_combos.items():
    ci = grid.indexOf(combo)
    if ci < 0:
        bad.append((name, "missing combo"))
        continue
    cr, cc, _, _ = grid.getItemPosition(ci)
    if cc != 0:
        bad.append((name, f"policy=({cr},{cc})"))
check("加载策略在 col0、每个工具一行", not bad, str(bad[:3]))

# 策略三档：关闭 / 按需 / 常驻
combo0 = next(iter(sw._tools_policy_combos.values()))
vals = [combo0.itemData(i) for i in range(combo0.count())]
check("加载策略三档 = 关闭/按需/常驻", vals == ["disabled", "lazy", "resident"], str(vals))

# 列宽一致性：所有行 col0 的 x 坐标应相同
xs = set()
for name, combo in sw._tools_policy_combos.items():
    idx = grid.indexOf(combo)
    r, c, _, _ = grid.getItemPosition(idx)
    xs.add(grid.cellRect(r, c).x())
check("加载策略列左右对齐", len(xs) <= 1, str(xs))

# 设置/介绍按钮在最右两列
btn_cols = set()
for i in range(grid.count()):
    it = grid.itemAt(i)
    wdg = it.widget() if it else None
    if isinstance(wdg, QPushButton) and wdg.text() in ("设置", "介绍"):
        r, c, _, _ = grid.getItemPosition(grid.indexOf(wdg))
        btn_cols.add(c)
check("设置/介绍按钮在 col2/col3", btn_cols <= {2, 3} and len(btn_cols) == 2, str(btn_cols))
print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
