"""파이프라인: 판정 → 근거 조립 → 답변 → 검증 (LangGraph).

START → classify → gate ─┬→ answer ⇄ tools → guard ─┬→ END
                         │      ↑___ 위반(2회 미만) ___|
                         └→ escalate ← 위반(2회째) ───┘
"""
import json
import os
import re
from functools import lru_cache
from typing import Annotated, List, Literal, Optional, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, Field

from context import build_context, lookup_policy, used_sections
from prompts import ESCALATE_MESSAGE, build_answer_prompt, build_escalate_prompt, build_route_prompt

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 모델은 교안과 같은 등급을 기본값으로 쓴다. 라우팅은 출력이 짧아 비용 최적화 등급, 답변은 한 등급 위.
ROUTE_MODEL = os.environ.get("ROUTE_MODEL", "gpt-5.6-luna")
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "gpt-5.6-terra")
# 교안의 이 등급 모델은 추론 모드와 도구 호출을 함께 쓸 수 없어 reasoning_effort 를 끈다.
# 다른 모델을 쓸 때는 ANSWER_REASONING= (빈 값)으로 두면 이 인자를 넘기지 않는다.
ANSWER_REASONING = os.environ.get("ANSWER_REASONING", "none")

CONF_THRESHOLD = 0.5    # 이보다 확신도가 낮으면 넘긴다
MAX_TURNS = 3           # 도구 호출 왕복 상한
MAX_ATTEMPTS = 2        # 가드레일 위반 시 답변 생성 시도 상한


# ── 도구: 카테고리별 근거를 조회한다 (근거 조립) ──────────────────────────────
@tool
def get_order_info() -> str:
    """주문·구성 선택 근거를 조회한다. 구성·가격, 현금 취급, 메시지 카드, 선물 포장, 결제, 주문 변경·취소 문의에 쓴다."""
    return lookup_policy("get_order_info")


@tool
def get_product_info() -> str:
    """상품·사용법 근거를 조회한다. 지압봉·조화 장식·용돈박스의 사양, 소재, 사용법, 주의사항, 원산지 문의에 쓴다."""
    return lookup_policy("get_product_info")


@tool
def get_shipping_info() -> str:
    """배송·포장 근거를 조회한다. 발송 시기, 택배사, 배송 기간, 배송비, 배송지, 발송 지연, 포장 문의에 쓴다."""
    return lookup_policy("get_shipping_info")


@tool
def get_return_info() -> str:
    """교환·반품·환불 근거를 조회한다. 신청 방법, 가능 기간, 반품 배송비, 하자·오배송, 환불 시기 문의에 쓴다."""
    return lookup_policy("get_return_info")


@tool
def escalate_to_agent(reason: str) -> str:
    """상담원에게 이관한다. 업무 문서 6장의 이관 사유에 해당하거나 문서에 근거가 없는 문의에 쓴다."""
    return json.dumps({"escalated": True, "reason": reason, "message": ESCALATE_MESSAGE},
                      ensure_ascii=False)


TOOLS = [get_order_info, get_product_info, get_shipping_info, get_return_info, escalate_to_agent]
TOOL_NAMES = {t.name for t in TOOLS}


# ── 모델 ────────────────────────────────────────────────────────────────────
class RouteDecision(BaseModel):
    """고객 문의 한 건에 대한 분류 결과."""
    category: Literal["ORDER", "PRODUCT", "SHIPPING", "RETURN_REFUND", "OTHER"] = Field(
        description="문의를 배정할 카테고리. 5개 값 중 하나만 사용한다.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="판단의 확신도. 두 카테고리 사이에서 애매하면 0.5 미만으로 낮춘다.")
    reason: str = Field(description="그 카테고리로 판단한 근거를 한 문장으로.")


@lru_cache(maxsize=1)
def _route_llm():
    return init_chat_model(ROUTE_MODEL, model_provider="openai", temperature=0,
                           timeout=60, max_retries=2).with_structured_output(RouteDecision)


@lru_cache(maxsize=1)
def _agent_llm():
    extra = {"reasoning_effort": ANSWER_REASONING} if ANSWER_REASONING else {}
    return init_chat_model(ANSWER_MODEL, model_provider="openai", temperature=0,
                           timeout=60, max_retries=2, **extra).bind_tools(TOOLS)


@lru_cache(maxsize=1)
def _reply_llm():
    """범위 밖 이관 답변용. 도구 없이 짧은 문장만 만든다."""
    extra = {"reasoning_effort": ANSWER_REASONING} if ANSWER_REASONING else {}
    return init_chat_model(ANSWER_MODEL, model_provider="openai", temperature=0,
                           timeout=60, max_retries=2, **extra)


@lru_cache(maxsize=1)
def _route_prompt():
    return build_route_prompt()


# ── 검증: 답변 속 숫자의 출처를 거꾸로 찾는다 ──────────────────────────────────
# 숫자는 단위와 함께 비교한다. '1~2일'의 2가 목록 번호 '2.'와 겹쳐 통과하는 일을 막기 위해서다.
_UNIT_RE = r"(영업일|송이|문장|시간|cm|원|일|장|자|시|개|g|주|%)"
_UNIT_ALIAS = {"영업일": "일"}      # 문서의 '3영업일'과 답변의 '3일'을 같게 본다


def numbers_in(text):
    """(수, 단위) 쌍을 뽑는다. 쉼표를 지우고, '3만원'은 (30000, 원), '1~2일'은 (1, 일)·(2, 일)."""
    t = re.sub(r"(?<=\d),(?=\d)", "", str(text))
    pairs = {(int(m.group(1)) * 10000, "원") for m in re.finditer(r"(\d+)\s*만\s*원?", t)}
    for m in re.finditer(rf"(\d+)(?:\s*~\s*(\d+))?\s*{_UNIT_RE}", t):
        unit = _UNIT_ALIAS.get(m.group(3), m.group(3))
        pairs.add((int(m.group(1)), unit))
        if m.group(2):
            pairs.add((int(m.group(2)), unit))
    return pairs


def allowed_numbers(question, results):
    """허용 집합: 공통 문서 + 조회 결과 + 고객 문의의 (수, 단위), 그리고 큰 금액의 한 단계 합·차."""
    src = "\n".join([build_context([]), *results.values(), question])
    base = numbers_in(src)
    won = sorted(n for n, u in base if u == "원" and n >= 1000)
    derived = {(a + b, "원") for a in won for b in won} | {(a - b, "원") for a in won for b in won if a > b}
    return base | derived


def guardrail(answer, results, question=""):
    """답변에 근거 없는 숫자가 있으면 위반으로 표시한다. 조회하지 않은 장의 값을 쓴 경우도 걸린다."""
    allowed = allowed_numbers(question, results)
    found = numbers_in(answer)
    unsupported = sorted(p for p in found if p not in allowed)
    violations = []
    if unsupported:
        shown = [f"{n:,}{u}" for n, u in unsupported]
        violations.append({"type": "출처 불명 수치",
                           "detail": f"공통 문서·조회 결과·문의에 없는 숫자: {shown}"})
    return {"ok": not violations, "violations": violations,
            "numbers_in_answer": [f"{n:,}{u}" for n, u in sorted(found)],
            "unsupported": [f"{n:,}{u}" for n, u in unsupported]}


# ── 상태와 노드 ──────────────────────────────────────────────────────────────
class AgentState(TypedDict, total=False):
    question: str
    category: str
    confidence: float
    reason: str
    action: str                      # HANDLE / ANSWER / RETRY / ESCALATE
    escalate_reason: Optional[str]   # low_confidence / out_of_scope / no_evidence / guardrail / recursion
    tools: List[str]                 # 실제로 호출한 도구
    results: dict                    # 도구 이름 → 조회 결과
    answer: str
    guardrail: dict
    attempts: int


class ToolState(TypedDict, total=False):
    messages: Annotated[list, add_messages]    # 덮어쓰지 않고 이어 붙는다


def classify(state: AgentState) -> AgentState:
    """① 카테고리 판정 — 어느 카테고리인지만 정한다."""
    d = _route_llm().invoke([("system", _route_prompt()),
                             ("human", f"고객 문의: {state['question']}")])
    return {"category": d.category, "confidence": d.confidence, "reason": d.reason}


def gate(state: AgentState) -> AgentState:
    """② 넘기기 판단 — 확신이 없거나 범위 밖이면 넘긴다. 카테고리를 고르는 일과 분리했다."""
    if state["confidence"] < CONF_THRESHOLD:
        return {"action": "ESCALATE", "escalate_reason": "low_confidence"}
    if state["category"] == "OTHER":
        return {"action": "ESCALATE", "escalate_reason": "out_of_scope"}
    return {"action": "HANDLE"}


def build_tool_graph(get_llm=None):
    """③ 근거 조립 + 답변 — 모델이 도구를 고르고, 실행은 코드가 한다."""
    get_llm = get_llm or _agent_llm

    def agent(state: ToolState) -> ToolState:
        return {"messages": [get_llm().invoke(state["messages"])]}

    g = StateGraph(ToolState)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(TOOLS))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile()


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)


def _tool_calls(messages):
    """ToolMessage 에서 (호출한 도구 순서, 도구별 조회 결과) 를 꺼낸다."""
    used, results = [], {}
    for m in messages:
        name = getattr(m, "name", None)
        if getattr(m, "type", "") == "tool" and name in TOOL_NAMES:
            if name not in used:
                used.append(name)
            results[name] = _text(m.content)
    return used, results


def escalation_reply(question):
    """범위 밖 이관 답변: 불편 인정·사유·연결 문구를 담는다.
    생성이 비었거나, 가드레일을 못 넘거나, 연결 문구로 끝나지 않으면 고정 문구로 돌아간다."""
    msg = _reply_llm().invoke([("system", build_escalate_prompt()), ("human", question)])
    text = _text(msg.content).strip()
    if not text or not text.endswith(ESCALATE_MESSAGE) or not guardrail(text, {}, question)["ok"]:
        return ESCALATE_MESSAGE
    return text


def build_agent(classify_fn=None, get_llm=None):
    tool_app = build_tool_graph(get_llm)

    def node_answer(state: AgentState) -> AgentState:
        system = build_answer_prompt(state["category"])
        prev = state.get("guardrail")
        if prev and not prev["ok"]:      # 재생성이면 무엇이 문제였는지 알려 준다
            system += ("\n[이전 답변의 문제] " + prev["violations"][0]["detail"] +
                       "\n조회 결과에 없는 숫자는 쓰지 말고 다시 답한다.\n")
        init = {"messages": [("system", system), ("human", state["question"])]}
        attempts = state.get("attempts", 0) + 1
        try:
            out = tool_app.invoke(init, {"recursion_limit": 2 * MAX_TURNS + 1})
        except GraphRecursionError:      # 상한에 걸린 경우만 넘긴다. 다른 예외는 일부러 잡지 않는다.
            return {"action": "ESCALATE", "escalate_reason": "recursion", "attempts": attempts,
                    "tools": ["escalate_to_agent"], "results": {}, "answer": ESCALATE_MESSAGE}
        used, results = _tool_calls(out["messages"])
        text = _text(out["messages"][-1].content).strip()
        if "escalate_to_agent" in used:  # 모델이 근거 없음·이관 사유로 판단해 넘긴 경우
            return {"action": "ESCALATE", "escalate_reason": "no_evidence", "attempts": attempts,
                    "tools": used, "results": results, "answer": text or ESCALATE_MESSAGE}
        return {"action": "ANSWER", "attempts": attempts,
                "tools": used, "results": results, "answer": text}

    def node_guard(state: AgentState) -> AgentState:
        g = guardrail(state["answer"], state.get("results", {}), state["question"])
        return {"guardrail": g, "action": "ANSWER" if g["ok"] else "RETRY"}

    def node_escalate(state: AgentState) -> AgentState:
        tools = list(state.get("tools", []))
        if "escalate_to_agent" not in tools:
            tools.append("escalate_to_agent")
        reason = state.get("escalate_reason") or "guardrail"
        answer = escalation_reply(state["question"]) if reason == "out_of_scope" else ESCALATE_MESSAGE
        return {"action": "ESCALATE", "answer": answer, "tools": tools, "escalate_reason": reason}

    def after_gate(state: AgentState) -> str:
        return "answer" if state["action"] == "HANDLE" else "escalate"

    def after_answer(state: AgentState) -> str:
        return END if state["action"] == "ESCALATE" else "guard"

    def after_guard(state: AgentState) -> str:
        if state["action"] == "ANSWER":
            return END
        return "answer" if state.get("attempts", 0) < MAX_ATTEMPTS else "escalate"

    g = StateGraph(AgentState)
    g.add_node("classify", classify_fn or classify)
    g.add_node("gate", gate)
    g.add_node("answer", node_answer)
    g.add_node("guard", node_guard)
    g.add_node("escalate", node_escalate)
    g.add_edge(START, "classify")
    g.add_edge("classify", "gate")
    g.add_conditional_edges("gate", after_gate, {"answer": "answer", "escalate": "escalate"})
    g.add_conditional_edges("answer", after_answer, {"guard": "guard", END: END})
    g.add_conditional_edges("guard", after_guard, {"answer": "answer", "escalate": "escalate", END: END})
    g.add_edge("escalate", END)
    return g.compile()


@lru_cache(maxsize=1)
def _app():
    return build_agent()


def stream_agent(question):
    """노드가 끝날 때마다 (노드 이름, 그 노드가 돌려준 상태 변경)을 내보낸다.
    화면이 실제 진행 상태를 그릴 때 쓴다. 파이프라인 로직은 run_agent 와 같은 그래프다."""
    for chunk in _app().stream({"question": question}, stream_mode="updates"):
        for node, update in chunk.items():
            yield node, update


def require_api_key():
    """키 없이 모델을 부르면 긴 오류 추적이 나오므로, 명령줄 도구는 먼저 이 안내를 보여 준다."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY가 없어요. .env.example을 .env로 복사해 키를 채워 주세요.\n"
                         "키 없이 실행할 수 있는 것: python context.py, python agent.py --graph")


def run_agent(question):
    """문의 하나를 파이프라인에 통과시킨다. 로그로 쓸 수 있게 상태를 그대로 돌려준다."""
    out = _app().invoke({"question": question})
    return {"question": question, "sections": used_sections(out.get("tools", [])), **out}


if __name__ == "__main__":
    import sys
    if "--graph" in sys.argv:
        print(build_agent().get_graph().draw_mermaid())
    else:
        require_api_key()
        q = " ".join(a for a in sys.argv[1:]) or "택배사는 어디고 보통 며칠 걸려요?"
        r = run_agent(q)
        print(f'Q: {q}\nA: {r["answer"]}')
        print(f'   category={r.get("category")} conf={r.get("confidence")} action={r["action"]} '
              f'tools={r.get("tools")} sections={r["sections"]} guardrail={r.get("guardrail", {}).get("ok")}')
