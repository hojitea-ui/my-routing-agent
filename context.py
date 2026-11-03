"""근거 문서(docs/policy.md)를 장 단위로 쪼개고, 호출된 도구에 맞는 장만 조립한다."""
import re
from pathlib import Path

POLICY_PATH = Path(__file__).parent / "docs" / "policy.md"

# 도구 하나가 읽어 오는 장. 장 번호는 policy.md 의 '## N.' 과 일대일로 대응한다.
SECTION_MAP = {
    "get_order_info": ["2"],      # 2장 주문·구성 선택
    "get_product_info": ["3"],    # 3장 상품·사용법
    "get_shipping_info": ["4"],   # 4장 배송·포장
    "get_return_info": ["5"],     # 5장 교환·반품·환불
    "escalate_to_agent": [],      # 넘기기는 근거를 읽지 않는다
}

# 어떤 도구를 불러도 항상 포함: 표기 규약·기본 원칙·분류·범위 밖과 이관·응대 태도
ALWAYS = ["이 문서를 쓰는 방법", "0", "1", "6", "7"]


def split_sections(text):
    """'## ' 제목 단위로 쪼갠다. 키는 장 번호(문자열). 부록은 프롬프트에 넣지 않으므로 뺀다."""
    parts = re.split(r"^## ", text, flags=re.M)
    out = {"_header": parts[0].strip()}
    for p in parts[1:]:
        title = p.split("\n", 1)[0].strip()
        if title.startswith("부록"):
            continue
        m = re.match(r"(\d+)\.", title)
        out[m.group(1) if m else title] = "## " + p.rstrip()
    return out


def load_sections(path=POLICY_PATH):
    return split_sections(Path(path).read_text(encoding="utf-8"))


SECTIONS = load_sections()


def section_title(key, secs=None):
    secs = SECTIONS if secs is None else secs
    return secs[key].split("\n", 1)[0].replace("## ", "")


def lookup_policy(tool, secs=None):
    """도구 하나가 돌려주는 근거: 그 도구에 연결된 장만 이어 붙인다."""
    secs = SECTIONS if secs is None else secs
    return "\n\n".join(secs[k] for k in SECTION_MAP.get(tool, []) if k in secs)


def used_sections(tools, secs=None):
    """호출된 도구가 근거로 읽은 장의 (번호, 제목) 목록. 데모 화면에 보여 줄 때 쓴다."""
    secs = SECTIONS if secs is None else secs
    keys = [k for t in tools for k in SECTION_MAP.get(t, []) if k in secs]
    return [(k, section_title(k, secs)) for k in dict.fromkeys(keys)]


def build_context(tools=(), secs=None):
    """공통 장 + 호출된 도구의 장으로 프롬프트용 컨텍스트를 만든다."""
    secs = SECTIONS if secs is None else secs
    keys = [k for k in ALWAYS if k in secs]
    keys += [k for t in tools for k in SECTION_MAP.get(t, []) if k in secs and k not in keys]
    return "\n\n".join([secs["_header"]] + [secs[k] for k in keys])


if __name__ == "__main__":
    full = POLICY_PATH.read_text(encoding="utf-8")
    print("문서 전체:", len(full), "자")
    print("\n[파싱된 장]")
    for k, v in SECTIONS.items():
        head = "(문서 정보)" if k == "_header" else section_title(k)
        print(f"  {k:22s} {len(v):5d}자  {head}")

    print("\n[도구별 컨텍스트 길이]")
    for t in SECTION_MAP:
        n = len(build_context([t]))
        print(f"  {t:18s} {n:5d}자  전문 대비 {100 * n / len(full):5.1f}%")

    # 다른 장에만 있는 값이 섞여 들어오지 않았는지 검사한다.
    print("\n[분리 검사] 해당 도구 컨텍스트에만 있어야 하는 값")
    checks = [("왕복 6,000원", "get_return_info", "반품 배송비"),
              ("선물 포장은 유료", "get_order_info", "선물 포장 금액"),
              ("CJ대한통운", "get_shipping_info", "택배사"),
              ("약 13cm", "get_product_info", "지압봉 길이")]
    for needle, owner, label in checks:
        where = [t for t in SECTION_MAP if needle in build_context([t])]
        ok = where == [owner]
        print(f"  {'분리됨 ✅' if ok else '섞임 ⚠️'}  {label} ({needle}) → {where}")
