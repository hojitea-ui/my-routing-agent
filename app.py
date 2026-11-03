"""JADE//COMMERCE 데모 화면: 문의를 넣으면 분류·근거 조회·답변·검증의 실제 진행 상태와 결과 리포트를 보여 준다.

실행: streamlit run app.py
디자인 토큰은 assets/style.css 의 :root 변수와 .streamlit/config.toml 에서만 정의한다.
"""
import html
import os
import re
import time
from pathlib import Path

import streamlit as st

from agent import ANSWER_MODEL, CONF_THRESHOLD, MAX_ATTEMPTS, ROUTE_MODEL, stream_agent
from context import SECTIONS, used_sections
from prompts import load_fewshot

ASSETS = Path(__file__).parent / "assets"
APP_VERSION = "1.0"
CHANNEL = "NAVER SMARTSTORE"
DOC_NAME = "STORE OPERATIONS MANUAL · docs/policy.md"
TERMS_DATES = "공지 2026-05-26 · 적용 2026-06-29"
esc = html.escape

# 아이콘은 가늘고 기하학적인 선으로만 그린다.
ICONS = {
    "ORDER": '<path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/>',
    "PRODUCT": '<path d="M3 3h8l10 10-8 8L3 11V3z"/><circle cx="7.5" cy="7.5" r="1.3"/>',
    "SHIPPING": '<path d="M2 6h11v9H2z"/><path d="M13 9h4l3 3v3h-7z"/><circle cx="6" cy="17.5" r="1.8"/><circle cx="16" cy="17.5" r="1.8"/>',
    "RETURN_REFUND": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
    "OTHER": '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
}
# (인덱스, 영문 제목, 한글 제목, 한 줄 설명)
CATEGORY_INFO = {
    "ORDER": ("01/04", "ORDER & OPTIONS", "주문·구성 선택", "구성·가격, 메시지 카드, 선물 포장, 결제"),
    "PRODUCT": ("02/04", "PRODUCT & USAGE", "상품·사용법", "지압봉·조화·용돈박스 사양, 사용법, 인증"),
    "SHIPPING": ("03/04", "SHIPPING & PACKAGING", "배송·포장", "발송 시기, 택배사, 배송비, 배송지"),
    "RETURN_REFUND": ("04/04", "RETURNS & REFUNDS", "교환·반품·환불", "신청 기간, 반품 배송비, 하자, 환불"),
    "OTHER": ("OOS", "OUT OF SCOPE", "범위 밖", "상담원 이관 대상 문의"),
}
CARD_ORDER = ["ORDER", "PRODUCT", "SHIPPING", "RETURN_REFUND", "OTHER"]
TOOL_LABEL = {"get_order_info": "주문·구성 선택 근거 조회", "get_product_info": "상품·사용법 근거 조회",
              "get_shipping_info": "배송·포장 근거 조회", "get_return_info": "교환·반품·환불 근거 조회",
              "escalate_to_agent": "상담원 이관"}
ESCALATE_LABEL = {"low_confidence": "분류 확신도가 낮아 이관했어요",
                  "out_of_scope": "응대 범위 밖이라 이관했어요",
                  "no_evidence": "문서에 근거가 없거나 이관 사유라서 모델이 이관했어요",
                  "guardrail": "근거 없는 숫자가 반복 감지되어 이관했어요",
                  "recursion": "도구 호출 횟수가 상한을 넘어 이관했어요"}
# 실제 그래프 노드와 일대일로 대응한다.
STEP_DEFS = [("classify", "01", "CATEGORY CLASSIFICATION", "카테고리 분류"),
             ("gate", "02", "ROUTING GATE", "이관 판정"),
             ("answer", "03", "EVIDENCE & GENERATION", "근거 조회 · 답변 생성"),
             ("guard", "04", "VALIDATION", "숫자 검증"),
             ("escalate", "05", "HANDOFF", "상담원 이관")]

# 예시 문의는 평가에 쓰지 않는 예시용(fewshot) 문항과 데모 전용 문항만 쓴다.
FEWSHOT = load_fewshot()
CARD_QUERY = {}
for _it in FEWSHOT:
    CARD_QUERY.setdefault(_it["category"], _it["question"])
EXAMPLES = [it["question"] for it in FEWSHOT][:6] + [
    "선물 포장하고 메시지 카드까지 넣으면 지압봉 세트가 얼마예요?",
    "이거 쓰면 관절염이 나을까요?",
]


# ── HTML 조각 (한 줄로 만든다: 빈 줄이 있으면 마크다운이 HTML 블록을 끊는다) ─────────
def label(n, name, meta=""):
    m = f'<span class="meta">{esc(meta)}</span>' if meta else ""
    text = f"<b>[</b>{name}<b>]</b>" if n is None else f"<b>[{n:02d}</b> / {name}<b>]</b>"
    return f'<div class="jc-label"><span>{text}</span>{m}</div>'


def header_html(online):
    dot = '<span class="jc-dot"></span><strong>ONLINE</strong>' if online else \
        '<span class="jc-dot off"></span><strong>NO API KEY</strong>'
    return ('<div class="jc-header"><div class="jc-brand"><div class="jc-mark" aria-hidden="true">◆</div>'
            '<div><div class="jc-logo">JADE<i>//</i>COMMERCE</div>'
            '<div class="jc-sub">COMMERCE OPERATIONS SYSTEM</div></div></div>'
            f'<div class="jc-sys"><span>SYSTEM STATUS</span><span>{dot}</span>'
            f'<span class="ver">V{APP_VERSION} · CH/NAVER</span></div></div>')


HERO_HTML = ('<div class="jc-hero"><h1>고객 문의, 무엇이든 물어보세요.</h1>'
             '<p>네이버 스마트스토어 판매 이용약관과 스토어 업무 문서를 근거로 정확하게 안내합니다.</p></div>'
             '<div class="jc-channels" role="group" aria-label="판매 채널">'
             '<span class="jc-chip on" aria-current="true">네이버 스마트스토어 <small>ACTIVE</small></span>'
             '<span class="jc-chip" aria-disabled="true">쿠팡 <small>COMING SOON</small></span>'
             '<span class="jc-chip" aria-disabled="true">자사몰 <small>COMING SOON</small></span></div>')


def card_html(cat, active):
    idx, en, kr, desc = CATEGORY_INFO[cat]
    cls = "jc-card" + (" oos" if cat == "OTHER" else "") + (" is-active" if active else "")
    return (f'<div class="{cls}"><div class="jc-card-top"><span class="jc-idx">{idx}</span>'
            f'<span class="jc-ico" aria-hidden="true"><svg viewBox="0 0 24 24">{ICONS[cat]}</svg></span></div>'
            f'<div class="jc-card-en">{en}</div><div class="jc-card-kr">{kr}</div>'
            f'<div class="jc-card-desc">{desc}</div></div>')


def stepper_html(steps):
    items = []
    for key, idx, en, kr in STEP_DEFS:
        stt = steps["status"][key]
        meta = stt
        if steps["time"].get(key):
            meta += f' · {steps["time"][key]:.1f}s'
        if key == "answer" and steps.get("attempts", 0) > 1:
            meta += f' · ×{steps["attempts"]}'
        items.append(f'<li class="jc-step is-{stt.lower()}"><span class="jc-step-dot" aria-hidden="true"></span>'
                     f'<span class="jc-step-idx">{idx}</span><span class="jc-step-en">{en}</span>'
                     f'<span class="jc-step-kr">{kr}</span><span class="jc-step-st">{meta}</span></li>')
    return f'<ol class="jc-steps" aria-live="polite" aria-label="에이전트 처리 단계">{"".join(items)}</ol>'


def clause_tags(keys):
    """조회된 장의 꼬리표에서 약관·이용규칙 조항을 순서대로 뽑는다."""
    seen = []
    for k in keys:
        for grp in re.findall(r"\[([^\]]+)\]", SECTIONS[k]):
            for part in grp.split(","):
                part = part.strip()
                if part.startswith(("약관", "판매이용규칙")) and part not in seen:
                    seen.append(part)
    return seen


def rs(n, name, body):
    return (f'<div class="jc-rs"><div class="jc-rs-h"><i>[{n}]</i>{name}</div>'
            f'<div class="jc-rs-b">{body}</div></div>')


def answer_block(inner):
    """최종 답변 영역. 질문 입력창 바로 아래에 놓이며, 스크롤 이동의 기준점(앵커)을 함께 둔다."""
    return '<div id="jc-answer-anchor"></div>' + label(2, "FINAL ANSWER", f"CH / {CHANNEL}") + inner


PENDING_HTML = ('<section class="jc-final" role="status" aria-live="polite"><div class="jc-final-head">'
                '<span>FINAL ANSWER</span><span class="jc-tag light proc">PROCESSING</span></div>'
                '<div class="jc-wait">분류하고 근거를 읽는 중이에요. 아래 단계가 실제 진행 상태예요.</div></section>')


def answer_html(r):
    """질문에 대한 답. 실행 로그보다 먼저, 가장 눈에 띄게 보여 준다."""
    tag = ('<span class="jc-tag light info">HANDOFF</span>' if r["action"] == "ESCALATE"
           else '<span class="jc-tag light ok">ANSWERED</span>')
    ans = esc(r["answer"]).replace("\n", "<br>")
    return (f'<section class="jc-final" aria-label="최종 답변"><div class="jc-final-head">'
            f'<span>{esc(r["question"])}</span>{tag}</div><div class="jc-answer">{ans}</div></section>')


def log_html(r):
    """실행 로그: 질문 요약 · 카테고리와 채널 · 근거 조회 · 검증 결과 (이번 실행에서 실제로 일어난 일)."""
    cat = r.get("category")
    idx, en, kr, _ = CATEGORY_INFO.get(cat, ("-", "-", "-", ""))
    escalated = r["action"] == "ESCALATE"
    conf = r.get("confidence") or 0
    tools = r.get("tools", [])
    keys = [k for k, _ in r["sections"]]

    head = ('<div class="jc-report-head"><span class="t">EXECUTION LOG · '
            f'{r.get("elapsed", 0):.1f}s</span>'
            + ('<span class="jc-tag info">HANDOFF</span>' if escalated else '<span class="jc-tag ok">ANSWERED</span>')
            + '</div>')

    s1 = rs("01", "QUERY SUMMARY", f'<p class="jc-quote">{esc(r["question"])}</p>')

    route = "HANDOFF — " + esc(ESCALATE_LABEL.get(r.get("escalate_reason"), "이관")) if escalated else "ANSWER"
    reason = f'<dt>Reason</dt><dd class="jc-muted">{esc(r["reason"])}</dd>' if r.get("reason") else ""
    s2 = rs("02", "CATEGORY &amp; CHANNEL",
            '<dl class="jc-kv">'
            f'<dt>Category</dt><dd><strong>{kr}</strong> <span class="jc-muted">{en}</span></dd>'
            f'<dt>Confidence</dt><dd>{conf:.2f}<span class="jc-meter" aria-hidden="true"><i style="width:{conf * 100:.0f}%"></i></span>'
            f'<span class="jc-muted"> &nbsp;threshold {CONF_THRESHOLD}</span></dd>'
            f'<dt>Channel</dt><dd><strong>{CHANNEL}</strong> <span class="jc-muted">단일 채널 정책만 적용</span></dd>'
            f'<dt>Routing</dt><dd>{route}</dd>{reason}</dl>')

    if tools:
        tool_li = "".join(f'<li><code>{t}</code> {TOOL_LABEL.get(t, t)}</li>' for t in tools)
    else:
        tool_li = '<li class="jc-muted">호출한 도구 없음</li>'
    if keys:
        chips = "".join(f'<span class="jc-pill">{esc(t)}</span>' for _, t in r["sections"])
        clauses = clause_tags(keys)
        clause_chips = "".join(f'<span class="jc-pill m">{esc(c)}</span>' for c in clauses) or \
            '<span class="jc-muted">없음</span>'
        n_assumed = sum(len(re.findall(r"\[가정", SECTIONS[k])) for k in keys)
        docs = "".join(
            f'<details><summary>{esc(t)}</summary><pre>{esc(SECTIONS[k].split(chr(10), 1)[1]).replace(chr(10), "&#10;")}</pre></details>'
            for k, t in r["sections"])
        ev = (f'<dt>Sections</dt><dd><div class="jc-chips">{chips}</div></dd>'
              f'<dt>Clauses</dt><dd><div class="jc-chips">{clause_chips}</div>'
              f'<div class="jc-muted" style="margin-top:6px">조회된 장에 달린 근거 꼬리표예요. 이 스토어가 정한 값(가정) {n_assumed}건이 함께 들어 있어요.</div></dd>')
    else:
        docs, ev = "", '<dt>Sections</dt><dd class="jc-muted">조회한 근거 장이 없어요. 공통 장만 포함돼요.</dd>'
    s3 = rs("03", "EVIDENCE RETRIEVAL",
            '<dl class="jc-kv">'
            f'<dt>Tools</dt><dd><ul>{tool_li}</ul></dd>'
            f'<dt>Document</dt><dd>{DOC_NAME}<div class="jc-muted">원문 근거: 네이버 스마트스토어 판매 이용약관 ({TERMS_DATES})</div></dd>'
            f'{ev}</dl>{docs}')

    g = r.get("guardrail")
    if g is None:
        body = ('<span class="jc-tag light info">SKIPPED</span>'
                '<p class="jc-muted" style="margin:10px 0 0">이관 응답은 숫자 검증 단계를 거치지 않아요. '
                '(범위 밖 이관 답변은 생성할 때 숫자만 한 번 검사해요.)</p>')
    else:
        chip = '<span class="jc-tag light ok">PASS</span>' if g["ok"] else '<span class="jc-tag light err">FAIL</span>'
        un = (f'<dt>Unsupported</dt><dd>{esc(", ".join(g["unsupported"]))}</dd>') if g["unsupported"] else ""
        body = (f'{chip}<dl class="jc-kv" style="margin-top:12px">'
                f'<dt>Numbers</dt><dd>{esc(", ".join(g["numbers_in_answer"])) or "없음"}</dd>{un}'
                f'<dt>Attempts</dt><dd>{r.get("attempts", 1)} / {MAX_ATTEMPTS} '
                '<span class="jc-muted">위반이면 한 번 다시 쓰고, 그래도 위반이면 이관해요.</span></dd></dl>')
    s4 = rs("04", "VALIDATION RESULT", body)
    return f'<section class="jc-report" aria-label="실행 로그">{head}{s1}{s2}{s3}{s4}</section>'


# ── 실제 노드 상태로 스테퍼를 갱신한다 (가짜 진행 없음) ───────────────────────────────
def new_steps():
    return {"status": {k: "WAITING" for k, *_ in STEP_DEFS}, "time": {}, "attempts": 0}


def advance(steps, node, update, state):
    """방금 끝난 노드의 결과(action)로 다음에 실제로 실행될 노드를 표시한다."""
    s = steps["status"]
    s[node] = "COMPLETED"
    steps["attempts"] = state.get("attempts", 0)
    if node == "classify":
        s["gate"] = "PROCESSING"
    elif node == "gate":
        if update["action"] == "HANDLE":
            s["answer"] = "PROCESSING"
        else:
            s["escalate"] = "PROCESSING"
            s["answer"] = s["guard"] = "SKIPPED"
    elif node == "answer":
        if update["action"] == "ESCALATE":      # 모델이 도구로 이관한 경우
            s["escalate"] = "HANDOFF"
            s["guard"] = "SKIPPED"
        else:
            s["guard"] = "PROCESSING"
    elif node == "guard":
        if update["action"] != "ANSWER":        # 위반: 다시 쓰거나 이관
            s["guard"] = "FAILED"
            if state.get("attempts", 0) < MAX_ATTEMPTS:
                s["answer"] = "PROCESSING"
            else:
                s["escalate"] = "PROCESSING"
        else:
            s["escalate"] = "SKIPPED"
    elif node == "escalate":
        s["escalate"] = "HANDOFF"


def run_with_stepper(query, slot, answer_slot):
    with answer_slot:
        ph_a = st.empty()
    ph_a.markdown(answer_block(PENDING_HTML), unsafe_allow_html=True)
    with slot:
        st.markdown(label(3, "AGENT PROCESSING", "LIVE NODE STATUS"), unsafe_allow_html=True)
        ph = st.empty()
    steps = new_steps()
    steps["status"]["classify"] = "PROCESSING"
    ph.markdown(stepper_html(steps), unsafe_allow_html=True)
    state, t_prev = {"question": query}, time.time()
    try:
        for node, update in stream_agent(query):
            now = time.time()
            steps["time"][node] = steps["time"].get(node, 0) + (now - t_prev)
            t_prev = now
            state.update(update)
            advance(steps, node, update, state)
            ph.markdown(stepper_html(steps), unsafe_allow_html=True)
    except Exception as e:      # 화면에는 원인을 그대로 보여 준다
        for k, v in steps["status"].items():
            if v == "PROCESSING":
                steps["status"][k] = "FAILED"
        ph.markdown(stepper_html(steps), unsafe_allow_html=True)
        ph_a.markdown(answer_block(
            f'<div class="jc-error" role="alert"><b>FAILED</b> &nbsp;처리하지 못했어요: '
            f'{esc(type(e).__name__)}: {esc(str(e))}</div>'), unsafe_allow_html=True)
        return None
    result = {**state, "question": query, "sections": used_sections(state.get("tools", [])),
              "steps": steps, "elapsed": sum(steps["time"].values())}
    ph_a.markdown(answer_block(answer_html(result)), unsafe_allow_html=True)
    return result


# ── 페이지 ───────────────────────────────────────────────────────────────────
st.set_page_config(page_title="JADE//COMMERCE", page_icon="◆", layout="wide", initial_sidebar_state="auto")
st.markdown(f"<style>{(ASSETS / 'style.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

if "history" not in st.session_state:
    st.session_state.history = []


def set_pending(q):
    st.session_state.pending = q


def clear_history():
    st.session_state.history = []


# 사이드바: 예시 문의 + 서비스 설명 + 참조 문서 기준일
with st.sidebar:
    st.markdown(label(None, "QUICK QUERIES"), unsafe_allow_html=True)
    for i, q in enumerate(EXAMPLES):
        st.button(q, key=f"ex{i}", on_click=set_pending, args=(q,), use_container_width=True)
    st.button("대화 지우기", key="clear", on_click=clear_history, use_container_width=True)
    st.markdown(
        '<dl class="jc-side-note">'
        '<dt>Service</dt><dd>용돈박스 + 옥 지압봉 스토어(가칭)의 고객 문의를 분류하고, 필요한 근거만 읽고, '
        '숫자까지 검증해서 답하는 AI 에이전트예요.</dd>'
        f'<dt>Reference</dt><dd>네이버 스마트스토어 판매 이용약관<br>{TERMS_DATES}</dd>'
        f'<dt>Models</dt><dd>분류 {ROUTE_MODEL}<br>답변 {ANSWER_MODEL}<br>이관 확신도 기준 {CONF_THRESHOLD}</dd>'
        '<dt>Note</dt><dd>가상의 스토어예요. 가격과 정책 일부는 약관에 없어 가정한 값이에요.</dd></dl>',
        unsafe_allow_html=True)

online = bool(os.environ.get("OPENAI_API_KEY"))
st.markdown(header_html(online), unsafe_allow_html=True)
st.markdown(label(1, "COMMERCE QUERY INTERFACE", "CH / NAVER SMARTSTORE") + HERO_HTML, unsafe_allow_html=True)
if not online:
    st.markdown('<div class="jc-error" role="alert"><b>NO API KEY</b> &nbsp;OPENAI_API_KEY가 없어요. '
                '프로젝트 폴더의 .env에 넣고 다시 실행해 주세요.</div>', unsafe_allow_html=True)

with st.form("query_form", clear_on_submit=True, border=False):
    c_in, c_btn = st.columns([6, 1.3], vertical_alignment="bottom")
    text = c_in.text_input("문의 입력", placeholder="예: 택배사는 어디고 보통 며칠 걸려요?",
                           label_visibility="collapsed")
    submitted = c_btn.form_submit_button("질문하기", type="primary", use_container_width=True, disabled=not online)
if submitted and not text.strip():
    st.caption("문의를 입력해 주세요.")

# 답변은 질문 입력창 바로 아래에 온다: 최종 답변 → 처리 상태 → 실행 로그 → 카테고리 카드 → 이전 기록
answer_slot, proc_slot, log_slot, cards_slot, hist_slot = [st.container() for _ in range(5)]
history = st.session_state.history

query = st.session_state.pop("pending", None) or (text.strip() if submitted and text.strip() else None)
if query:
    result = run_with_stepper(query, proc_slot, answer_slot)
    if result:
        history.append(result)
        # 카드나 사이드바에서 실행했을 때 결과가 화면 밖이면 답변 영역으로 부드럽게 이동한다.
        st.iframe(
            "<script>const d=window.parent.document,el=d.getElementById('jc-answer-anchor');"
            "if(el){const r=el.getBoundingClientRect();"
            "if(r.top<0||r.top>window.parent.innerHeight*0.7){el.scrollIntoView({behavior:'smooth',block:'start'});}}"
            "</script>", height=1)
elif history:
    with answer_slot:
        st.markdown(answer_block(answer_html(history[-1])), unsafe_allow_html=True)
    with proc_slot:
        st.markdown(label(3, "AGENT PROCESSING", "LAST RUN") + stepper_html(history[-1]["steps"]),
                    unsafe_allow_html=True)
else:
    with answer_slot:
        st.markdown(answer_block('<div class="jc-empty">아직 문의가 없어요. 위 입력창에 문의를 넣거나 '
                                 '카드·예시 문의를 눌러 보세요.</div>'), unsafe_allow_html=True)
    with proc_slot:
        st.markdown(label(3, "AGENT PROCESSING", "WAITING FOR QUERY") + stepper_html(new_steps()),
                    unsafe_allow_html=True)

with log_slot:
    if history:
        st.markdown(label(4, "EXECUTION LOG", "THIS RUN") + log_html(history[-1]), unsafe_allow_html=True)

active = history[-1].get("category") if history else None
with cards_slot:
    st.markdown(label(5, "ROUTING CATEGORIES", "CLICK TO RUN A SAMPLE QUERY"), unsafe_allow_html=True)
    with st.container(key="cards"):
        for col, cat in zip(st.columns(5), CARD_ORDER):
            with col, st.container(key=f"card_{cat}"):
                st.markdown(card_html(cat, cat == active), unsafe_allow_html=True)
                st.button(f"{CATEGORY_INFO[cat][2]} 예시 질문 실행", key=f"cardbtn_{cat}",
                          on_click=set_pending, args=(CARD_QUERY[cat],))

if len(history) > 1:
    with hist_slot:
        st.markdown(label(6, "PREVIOUS RUNS", f"{len(history) - 1} RUNS"), unsafe_allow_html=True)
        for r in reversed(history[:-1]):
            with st.expander(f'{CATEGORY_INFO.get(r.get("category"), ("", "", "-", ""))[2]} · {r["question"]}'):
                st.markdown(answer_html(r) + log_html(r), unsafe_allow_html=True)
