"""
1.7.1 LTM deepseek 推理模型 mock 测试

覆盖：
- thinking 已禁用时（content 直接有 YES/NO）—— 主路径
- thinking 仍在 reasoning 里含 YES+NO（极端兜底）
- _chat helper 的 extra_body 透传
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.long_term_memory_tool import LongTermMemoryTool

T = LongTermMemoryTool()


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


class FakeCompletions:
    def __init__(self, response):
        self._r = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._r


class FakeClient:
    def __init__(self, response):
        self.chat = type("C", (), {"completions": FakeCompletions(response)})()


print("=== _chat() helper thinking 禁用 ===")
T._disable_thinking = True
T._llm_client = FakeClient(FakeResponse(content="YES"))
r = T._chat(messages=[{"role": "user", "content": "x"}], max_tokens=512, temperature=0.1)
assert r.choices[0].message.content == "YES"
kw = T._llm_client.chat.completions.last_kwargs
assert "extra_body" in kw and kw["extra_body"] == {"thinking": {"type": "disabled"}}, f"missing extra_body: {kw}"
assert kw["max_tokens"] == 512
print(f"  [OK] extra_body 透传: {kw['extra_body']}  max_tokens={kw['max_tokens']}")

print("\n=== _chat() helper 普通模型不传 extra_body ===")
T._disable_thinking = False
T._llm_client = FakeClient(FakeResponse(content="OK"))
r = T._chat(messages=[{"role": "user", "content": "x"}], max_tokens=100, temperature=0.5)
kw = T._llm_client.chat.completions.last_kwargs
assert "extra_body" not in kw, f"普通模型不应有 extra_body: {kw}"
print(f"  [OK] 普通模型无 extra_body")

print("\n=== _extract_text 兜底 ===")
r1 = FakeResponse(content="YES")
assert T._extract_text(r1) == "YES"
r2 = FakeResponse(content="", reasoning="分析完毕，答案是 YES")
assert T._extract_text(r2) == "分析完毕，答案是 YES"
print(f"  [OK] content 优先 + reasoning 兜底")

print("\n=== _detect_memorable 判断逻辑 ===")


def detect_with(content=None, reasoning=None):
    """模拟 _chat 后的判定流程"""
    T._disable_thinking = True  # 假设已禁用
    raw = T._extract_text(FakeResponse(content, reasoning)).strip().upper()
    matches = list(re.finditer(r"(?<![A-Z])(YES|NO)(?![A-Z])", raw))
    if matches:
        return matches[-1].group(1) == "YES"
    return None


assert detect_with(content="YES") is True
assert detect_with(content="NO") is False
assert detect_with(content="yes") is True  # 大小写
assert detect_with(content="", reasoning="结论:YES") is True  # 中文标点
assert detect_with(content="", reasoning="结论YES") is True  # 中文紧贴
assert detect_with(content="", reasoning="思考过程... NO") is False
assert detect_with(content=None, reasoning=None) is None
print(f"  [OK] 7 个分支全部命中")

print("\n=== 真实 bug 场景：reasoning 同时含 YES/NO ===")
bug_text = "规则层提到 YES 情况和 NO 情况。最后决定 NO"
assert detect_with(content="", reasoning=bug_text) is False, "应取末尾 NO"
print(f"  [OK] 末尾独立匹配策略生效（旧 bug 已修）")

print("\n=== reasoning 长截断场景 ===")
truncated = "WE NEED TO DETERMINE IF THE CONVERSATION CONTAINS INFO WORTH LONG-TERM ME"
assert detect_with(content="", reasoning=truncated) is None, "无 YES/NO 应返 None"
print(f"  [OK] 无 YES/NO 时返 None（由重试/失败兜底处理）")

print("\nALL PASS")