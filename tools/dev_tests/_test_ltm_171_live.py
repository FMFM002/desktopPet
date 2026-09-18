"""
1.7.1 LTM 真机端到端测试

直连 deepseek-flash 跑 _detect_memorable，验证：
- extra_body={"thinking":{"type":"disabled"}} 真的关掉了 thinking
- LLM 真的能输出 YES/NO
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.long_term_memory_tool import LongTermMemoryTool

T = LongTermMemoryTool()

cases = [
    ("用户说自己叫FM，24小时运转，靠tokens维持",
     "我24h运作。不吃饭吃tokens。叫我FM。", True),
    ("用户问天气",
     "今天北京天气怎么样？", False),
    ("用户分享偏好（喜欢日式Galgame）",
     "我喜欢日式Galgame，电脑D盘Apps下收藏了一批G开头的", True),
]

for desc, text, expect in cases:
    result = T._detect_memorable(text)
    tag = "OK" if result == expect else "FAIL"
    print(f"  [{tag}] {desc}: 期望={expect} 实际={result}")
    if tag == "FAIL":
        sys.exit(1)

print("\n真机端到端 ALL PASS")