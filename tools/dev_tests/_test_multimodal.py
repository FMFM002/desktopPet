"""验证 1.6.0 多模态图片直传：内容组装、降级、清理"""
import os, sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

import src.core.database as _dbmod
_TMP = BASE / "_test_tmp_dbs"
_TMP.mkdir(exist_ok=True)
_dbmod.DB_DIR = _TMP

ok = True
def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


from src.core.session import ChatSession

s = ChatSession()
# 用真实图片
img = None
for cand in (BASE / "data" / "桌宠形象").rglob("*.jpg"):
    img = cand
    break
check("找到测试图片", img is not None, str(img))

# ── 1. 关闭多模态 → 纯文本 ──
print("== 1. 关闭多模态 ==")
s.attach_image(str(img))
s.set_multimodal(False)
content = s._build_user_content("看图")
check("未开启时仍是纯文本", isinstance(content, str) and content == "看图")
check("未开启时不会消耗待传图片", len(s._pending_images) == 1)

# ── 2. 开启多模态 → content 数组 ──
print("== 2. 开启多模态 ==")
s.set_multimodal(True)
content = s._build_user_content("帮我看看这张图")
check("组装成 content 数组", isinstance(content, list), type(content).__name__)
check("第一块是文本", content[0]["type"] == "text" and content[0]["text"] == "帮我看看这张图")
check("含 image_url 块", any(b.get("type") == "image_url" for b in content))
uri = [b for b in content if b.get("type") == "image_url"][0]["image_url"]["url"]
check("是 data:image/jpeg base64 URI", uri.startswith("data:image/jpeg;base64,"))
check("图片体积受控（<2MB）", len(uri) < 2_000_000, f"{len(uri)/1024:.0f}KB")
check("发送后待传列表已清空", len(s._pending_images) == 0)

# ── 3. 相对路径也能登记 ──
print("== 3. 相对路径登记 ==")
rel = "data/桌宠形象/deepseek/头像.jpg"
if (BASE / rel).exists():
    s.attach_image(rel)
    check("相对路径被解析为绝对路径",
          len(s._pending_images) == 1 and Path(s._pending_images[0]).is_absolute(),
          str(s._pending_images))
    s.clear_pending_images()
else:
    check("相对路径测试图片存在", False, rel)

# ── 4. 降级：剥掉消息里的图片块 ──
print("== 4. 模型不支持时的降级 ==")
s.messages = [
    {"role": "user", "content": [{"type": "text", "text": "看图"},
                                 {"type": "image_url", "image_url": {"url": "data:x"}}]},
    {"role": "assistant", "content": "好的"},
]
stripped = s._strip_images_from_messages()
check("检测到并剥离图片块", stripped is True)
check("降级后是纯文本", isinstance(s.messages[0]["content"], str))
check("保留了原文字", "看图" in s.messages[0]["content"])
check("重复调用返回 False", s._strip_images_from_messages() is False)

# 清理
try:
    s.db.delete_topic(s.topic_id)
except Exception:
    pass

print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
