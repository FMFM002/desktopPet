"""
1.7.0 LTM 推理模型兼容测试

离线覆盖 _extract_text + _detect_memorable 三类场景：
- content 有 YES/NO（普通模型）
- content 为空、reasoning_content 兜底（推理模型 max_tokens 截断）
- content 与 reasoning 都空（极端情况）
- 大小写、空格、末尾多符号
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.long_term_memory_tool import LongTermMemoryTool


class FakeMessage:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning


class FakeChoice:
    def __init__(self, content=None, reasoning=None):
        self.message = FakeMessage(content, reasoning)


class FakeResponse:
    def __init__(self, content=None, reasoning=None):
        self.choices = [FakeChoice(content, reasoning)]


T = LongTermMemoryTool()


def case(name, response, expect):
    got = T._detect_memorable.__wrapped__ if hasattr(T._detect_memorable, "__wrapped__") else T._detect_memorable
    # _detect_memorable 走真 LLM；这里只测 _extract_text 和判定逻辑
    raw = T._extract_text(response)
    content = raw.strip().upper()
    tokens = [x for x in content.split() if x]
    last = tokens[-1] if tokens else ""
    if last in ("YES", "NO"):
        result = last == "YES"
    elif "YES" in content and "NO" not in content:
        result = True
    elif "NO" in content and "YES" not in content:
        result = False
    else:
        result = None
    ok = result == expect
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: raw={raw[:40]!r} → {result} (期望 {expect})")
    return ok


print("=== _extract_text 兜底 ===")
passes = 0
total = 0

for n, r, e in [
    ("content 末尾 YES", FakeResponse(content="YES"), True),
    ("content 末尾 NO",  FakeResponse(content="NO"),  False),
    ("content 大小写",   FakeResponse(content="yes"), True),
    ("content 多余文字+末尾 YES",
        FakeResponse(content="Let me think. answer is YES"), True),
    ("content 多余文字+末尾 NO",
        FakeResponse(content="None worth remembering. NO"), False),
    ("★ 推理模型：content 空、reasoning 末尾 YES",
        FakeResponse(content="", reasoning="分析用户偏好后，答案是 YES"), True),
    ("★ 推理模型：content 空、reasoning 末尾 NO",
        FakeResponse(content=None, reasoning="没价值，回答 NO"), False),
    ("★ 极端：content 和 reasoning 都空",
        FakeResponse(content=None, reasoning=None), None),
    ("★ 极端：content 单独一个 yes（首词也是末词）",
        FakeResponse(content="yes"), True),
    ("★ 推理：reasoning 中混杂 YES NO，看末尾",
        FakeResponse(content="", reasoning="考虑中... NO 还是 YES 呢？最后决定 YES"),
        True),
]:
    total += 1
    if case(n, r, e):
        passes += 1

print(f"\n{passes}/{total} PASS")
sys.exit(0 if passes == total else 1)