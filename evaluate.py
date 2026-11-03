"""두 지표를 측정한다.

- 도구 호출 적절성: 실제로 호출한 도구 집합이 기대 도구와 정확히 일치하면 1점
- 답변 적절성: 필수 사실을 모두 담고 금지 사항을 하나도 어기지 않으면 1점 (LLM 채점기, 표현이 아니라 사실을 본다)

사용법:
  python evaluate.py --validate-judge          채점기를 모범 답안으로 먼저 검증
  python evaluate.py --tag baseline            평가용(eval) 문항으로 측정, results/baseline.json 저장
  python evaluate.py --tag t1 --limit 5        일부만 시험
"""
import argparse
import json
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import List

from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

from agent import run_agent

BASE = Path(__file__).parent
GOLDEN = BASE / "data" / "goldenset.json"
RESULTS = BASE / "results"
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", os.environ.get("ANSWER_MODEL", "gpt-5.6-terra"))


def pmap(fn, items, workers=8):
    """여러 건을 동시에 호출한다. 결과 순서는 입력 순서와 같다."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def load_items(split="eval"):
    items = json.loads(GOLDEN.read_text(encoding="utf-8"))["items"]
    return [it for it in items if it["split"] == split]


# ── 채점기 ───────────────────────────────────────────────────────────────────
class FactCheck(BaseModel):
    index: int = Field(description="필수 사실 번호(1부터)")
    present: bool = Field(description="답변이 이 사실을 담고 있으면 true")


class ForbidCheck(BaseModel):
    index: int = Field(description="금지 사항 번호(1부터)")
    violated: bool = Field(description="답변이 이 금지 내용을 말했으면 true")


class Judgement(BaseModel):
    """답변 한 건에 대한 채점 결과."""
    facts: List[FactCheck]
    forbids: List[ForbidCheck]
    note: str = Field(description="판단 근거를 한 줄로")


JUDGE_PROMPT = """\
너는 고객상담 답변 채점자다. 답변의 표현이 아니라 담긴 사실을 본다.

[필수 사실] 답변이 그 사실을 담고 있으면 present=true 다.
- 같은 뜻이면 표현이 달라도 인정한다. 숫자 표기가 달라도 같은 값이면 인정한다(3만원 = 30,000원).
- 사실이 일부만 담기거나 다른 값이 적혀 있으면 present=false 다.
[금지 사항] 답변이 그 내용을 말했으면 violated=true 다.
- 금지 내용을 부정하거나 바로잡는 문맥으로 언급한 것은 위반이 아니다(예: '무료가 아닙니다').
- 모든 필수 사실과 금지 사항에 대해 번호를 붙여 빠짐없이 판단한다.
"""


@lru_cache(maxsize=1)
def _judge_llm():
    return init_chat_model(JUDGE_MODEL, model_provider="openai", temperature=0,
                           timeout=60, max_retries=2).with_structured_output(Judgement)


def judge_answer(item, answer):
    """답변 한 건을 채점한다. 반환: (통과 여부, 누락된 사실, 어긴 금지 사항, 근거)"""
    facts = "\n".join(f"{i}. {f}" for i, f in enumerate(item["must_facts"], 1))
    forbids = "\n".join(f"{i}. {f}" for i, f in enumerate(item["forbid"], 1))
    human = (f"[고객 문의]\n{item['question']}\n\n[답변]\n{answer}\n\n"
             f"[필수 사실]\n{facts}\n\n[금지 사항]\n{forbids}")
    j = _judge_llm().invoke([("system", JUDGE_PROMPT), ("human", human)])
    present = {c.index: c.present for c in j.facts}
    violated = {c.index: c.violated for c in j.forbids}
    # 채점기가 빠뜨린 번호는 통과로 치지 않는다: 사실은 없음, 금지는 어김으로 본다.
    missing = [f for i, f in enumerate(item["must_facts"], 1) if not present.get(i, False)]
    broken = [f for i, f in enumerate(item["forbid"], 1) if violated.get(i, True)]
    return (not missing and not broken), missing, broken, j.note


def validate_judge(items):
    """채점기 자체 검증: 모범 답안은 전부 통과해야 하고, 엉뚱한 문항의 답안은 통과하면 안 된다."""
    refs = pmap(lambda it: judge_answer(it, it["reference"]), items)
    fails = [(it["id"], m, b, n) for it, (ok, m, b, n) in zip(items, refs) if not ok]
    print(f"[채점기 검증 1] 모범 답안 {len(items)}건 중 통과 {len(items) - len(fails)} / 실패 {len(fails)}")
    for cid, m, b, n in fails:
        print(f"  ⚠ {cid} 누락 {m} 위반 {b} — {n}")
    shifted = [(it, items[(i + 1) % len(items)]["reference"]) for i, it in enumerate(items)]
    wrong = pmap(lambda p: judge_answer(p[0], p[1]), shifted)
    leaked = [it["id"] for (it, _), (ok, *_r) in zip(shifted, wrong) if ok]
    print(f"[채점기 검증 2] 다른 문항의 답안 {len(items)}건 중 통과(=잘못 통과) {len(leaked)}건 {leaked}")
    return not fails and not leaked


def rejudge(tags):
    """저장된 측정 결과를 현재 평가셋 기준으로 다시 채점한다. 답변은 그대로 두고 기준만 바꿔 본다."""
    items = {it["id"]: it for it in json.loads(GOLDEN.read_text(encoding="utf-8"))["items"]}

    def redo(r):
        it = items[r["id"]]
        ok, missing, broken, note = judge_answer(it, r["answer"])
        return {**r, "expected_tools": it["expected_tools"],
                "tool_ok": set(r["tools"]) == set(it["expected_tools"]),
                "answer_ok": ok, "missing_facts": missing, "broken_forbid": broken, "judge_note": note}

    for tag in tags:
        data = json.loads((RESULTS / f"{tag}.json").read_text(encoding="utf-8"))
        new = pmap(redo, [r for r in data["items"] if "error" not in r])
        n = len(new)
        bad_t = [r["id"] for r in new if not r["tool_ok"]]
        bad_a = [r["id"] for r in new if not r["answer_ok"]]
        print(f"{tag:12s} 도구 {n - len(bad_t)}/{n} 답변 {n - len(bad_a)}/{n}  "
              f"도구 틀림 {bad_t} | 답변 틀림 {bad_a}")
        (RESULTS / f"{tag}.rejudged.json").write_text(
            json.dumps({**data, "rejudged": True, "items": new}, ensure_ascii=False, indent=2),
            encoding="utf-8")


# ── 측정 ─────────────────────────────────────────────────────────────────────
def run_item(item):
    rec = {"id": item["id"], "category": item["category"], "type": item["type"],
           "question": item["question"], "expected_tools": item["expected_tools"],
           "tags": item.get("tags", [])}
    try:
        r = run_agent(item["question"])
        rec.update(answer=r["answer"], tools=r.get("tools", []), action=r["action"],
                   pred_category=r.get("category"), confidence=r.get("confidence"),
                   escalate_reason=r.get("escalate_reason"), attempts=r.get("attempts"),
                   guardrail_ok=(r.get("guardrail") or {}).get("ok"))
        rec["tool_ok"] = set(rec["tools"]) == set(item["expected_tools"])
        ok, missing, broken, note = judge_answer(item, rec["answer"])
        rec.update(answer_ok=ok, missing_facts=missing, broken_forbid=broken, judge_note=note)
    except Exception as e:                      # 한 건의 오류가 전체 측정을 멈추지 않게 기록한다
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["tool_ok"] = rec["answer_ok"] = False
    return rec


def summarize(recs):
    n = len(recs)
    t = sum(r["tool_ok"] for r in recs)
    a = sum(r["answer_ok"] for r in recs)
    print(f"\n도구 호출 적절성 {t}/{n} ({100 * t / n:.1f}%) | 답변 적절성 {a}/{n} ({100 * a / n:.1f}%)")
    print(f"둘 다 통과 {sum(r['tool_ok'] and r['answer_ok'] for r in recs)}/{n}")
    by = defaultdict(list)
    for r in recs:
        by[r["category"]].append(r)
    print("\n[카테고리별]  도구 / 답변")
    for c, rs in by.items():
        print(f"  {c:14s} {sum(x['tool_ok'] for x in rs)}/{len(rs)}  {sum(x['answer_ok'] for x in rs)}/{len(rs)}")
    print("\n[유형별]  도구 / 답변")
    for ty in ("answer", "escalate"):
        rs = [r for r in recs if r["type"] == ty]
        if rs:
            print(f"  {ty:9s} {sum(x['tool_ok'] for x in rs)}/{len(rs)}  {sum(x['answer_ok'] for x in rs)}/{len(rs)}")
    done = [r for r in recs if "error" not in r]
    print("\n[행동]", dict(Counter(r["action"] for r in done)),
          "| [이관 사유]", dict(Counter(r["escalate_reason"] for r in done if r["escalate_reason"])))
    print("[가드레일 재생성(시도 2회 이상)]", sum(1 for r in done if (r.get("attempts") or 1) > 1), "건")
    print("[분류가 기대 카테고리와 다름]", [(r["id"], r["pred_category"]) for r in done
                                        if r["pred_category"] != r["category"]])
    errs = [r for r in recs if "error" in r]
    if errs:
        print(f"\n⚠ 오류로 측정하지 못한 문항 {len(errs)}건")
        for r in errs:
            print(f"  {r['id']}: {r['error'][:200]}")


def show_failures(recs):
    bad = [r for r in recs if not (r["tool_ok"] and r["answer_ok"]) and "error" not in r]
    print(f"\n[틀린 문항 {len(bad)}건]")
    for r in bad:
        print(f"\n▸ {r['id']} ({r['category']}) {r['question']}")
        if not r["tool_ok"]:
            exp, got = set(r["expected_tools"]), set(r["tools"])
            print(f"   도구 ✗ 기대 {sorted(exp)} 실제 {r['tools']}  (빠짐 {sorted(exp - got)} / 불필요 {sorted(got - exp)})")
        if not r["answer_ok"]:
            print(f"   답변 ✗ 누락 {r['missing_facts']} 위반 {r['broken_forbid']}")
        print(f"   답변: {r['answer']}")
        print(f"   분류 {r['pred_category']}({r['confidence']}) action={r['action']} 사유={r['escalate_reason']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run")
    ap.add_argument("--split", default="eval")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--validate-judge", action="store_true")
    ap.add_argument("--rejudge", nargs="+", metavar="TAG", help="저장된 결과를 현재 평가셋으로 다시 채점")
    args = ap.parse_args()
    if args.rejudge:
        rejudge(args.rejudge)
        return

    items = load_items(args.split)
    if args.limit:
        items = items[:args.limit]
    if args.validate_judge:
        validate_judge(items)
        return
    t0 = time.time()
    recs = pmap(run_item, items)
    summarize(recs)
    show_failures(recs)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.tag}.json"
    out.write_text(json.dumps({"tag": args.tag, "split": args.split, "models": {
        "route": os.environ.get("ROUTE_MODEL", "gpt-5.6-luna"),
        "answer": os.environ.get("ANSWER_MODEL", "gpt-5.6-terra"), "judge": JUDGE_MODEL},
        "items": recs}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out.relative_to(BASE)}  ({time.time() - t0:.0f}초)")


if __name__ == "__main__":
    main()
