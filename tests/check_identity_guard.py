"""Self-check for the Telegram Web chat-identity guard.

Run manually:  python3 tests/check_identity_guard.py
Kept out of the pytest suite on purpose: it extracts the pure helpers straight
from source so the guard can be verified without importing playwright.
"""
import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
src = (PROJECT_ROOT / "src" / "tgdeepseek" / "web_runtime.py").read_text(encoding="utf-8")
tree = ast.parse(src)

wanted = {"_TITLE_SPACES", "normalize_title", "hash_username", "chat_identity_matches", "peer_id_variants"}
def _pick(n):
    name = getattr(n, "name", None)
    if name in wanted:
        return True
    if isinstance(n, ast.Assign):
        return any(getattr(t, "id", "") == "_TITLE_SPACES" for t in n.targets)
    return False


nodes = [n for n in tree.body if _pick(n)]
assert len(nodes) == len(wanted), [getattr(n, "name", None) for n in nodes]

ns: dict = {}
module = ast.Module(body=nodes, type_ignores=[])
exec(compile("import re\nimport unicodedata\n" + ast.unparse(module), "<extract>", "exec"), ns)

m = ns["chat_identity_matches"]
n_title = ns["normalize_title"]
variants = ns["peer_id_variants"]

cases = [
    # name, expected, wanted_title, active, hash, visible, bound, want
    ("真实场景: hash 被规范化成 @group_one, 标题一致",
     ("-1234567890",), "示例群一 @group_one", "", "#@group_one", "示例群一 @group_one", set(), True),
    ("串群: 打开了另一个群",
     ("-1234567890",), "示例群一 @group_one", "-9876543210", "#-9876543210", "示例群二 @group_two", set(), False),
    ("串群: hash 是对方群数字 id",
     ("-1234567890",), "示例群一 @group_one", "", "#-9876543210", "", set(), False),
    ("数字 hash 命中, 标题未渲染",
     ("-1234567890",), "示例群一 @group_one", "", "#-1234567890", "", set(), True),
    ("active 行命中",
     ("-1234567890",), "示例群一 @group_one", "-1234567890", "", "", set(), True),
    ("id 命中但标题冲突 -> 拒绝",
     ("-1234567890",), "示例群一 @group_one", "-1234567890", "#-1234567890", "别的群", set(), False),
    ("@hash 但标题未渲染且未绑定 -> 拒绝",
     ("-1234567890",), "示例群一 @group_one", "", "#@group_one", "", set(), False),
    ("已绑定用户名 + 标题未渲染 -> 通过",
     ("-1234567890",), "示例群一 @group_one", "", "#@group_one", "", {"@group_one"}, True),
    ("绑定的是别的用户名 -> 拒绝",
     ("-1234567890",), "示例群一 @group_one", "", "#@other", "", {"@group_one"}, False),
    ("标题只差空格 -> 通过",
     ("-1234567890",), "示例群一 @group_one", "", "#@group_one", "示例群一@group_one", set(), True),
    ("标题相差实词 -> 拒绝",
     ("-1234567890",), "示例群一 @group_one", "", "#@group_one", "示例群三 @group_one", set(), False),
    ("无预期标题时只认 id",
     ("-1234567890",), "", "", "#-1234567890", "随便什么", set(), True),
    ("无预期标题 + @hash -> 拒绝 (没有标题证据)",
     ("-1234567890",), "", "", "#@group_one", "", set(), False),
    ("第二个群: hash 保持数字",
     ("-9876543210",), "示例群二 @group_two", "", "#-9876543210", "示例群二 @group_two", set(), True),
    ("第二个群: hash 也变 username",
     ("-9876543210",), "示例群二 @group_two", "", "#@group_two", "示例群二 @group_two", set(), True),
]

ok = True
for name, exp, wt, active, h, vis, bound, want in cases:
    got = m(exp, wt, active, h, vis, bound)
    flag = "PASS" if got == want else "FAIL"
    if got != want:
        ok = False
    print(f"[{flag}] {name}: got={got} want={want}")

print()
print("normalize_title 检查:",
      n_title("  示例群一　@group_one ") == n_title("示例群一 @group_one"))
print("peer_id_variants:", variants("-1234567890"), variants("#1234567890"))
print()
print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
