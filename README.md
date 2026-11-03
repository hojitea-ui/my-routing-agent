# MODE:N(모든) 고객응대 라우팅 에이전트

용돈박스 + 옥 지압봉을 네이버 스마트스토어에서 파는 **가상 스토어 MODE:N(모든)** 의 고객 문의를 분류하고, 카테고리별 근거 문서만 읽어 답하고, 답변 속 숫자를 검증하는 LangGraph 에이전트예요. 근거가 없거나 확신이 없으면 지어내지 않고 상담원에게 넘겨요.

```
문의 → 분류(classify) → 넘기기 판단(gate) → 근거 조회 + 답변(answer ⇄ tools) → 검증(guard)
                              ↓                        ↓
                       범위 밖 · 확신 낮음         근거 없음 → 이관(escalate)
```

![데모 화면](docs/images/demo_answer.jpg)

## 결과 한눈에 보기

평가용 25건을 같은 조건으로 두 번 측정한 최종 결과예요. 개선 과정과 틀린 건 분석은 [REPORT.md](REPORT.md)에 있어요.

| 지표 | 결과 |
|---|---|
| 도구 호출 적절성 (호출한 도구 집합이 기대 도구와 정확히 일치) | 24/25 (96%), 두 번 모두 |
| 답변 적절성 (필수 사실을 모두 담고 금지 사항을 어기지 않음) | 21/25 (84%), 두 번 모두 |
| 넘기기가 정답인 8건 | 도구 8/8 |

## 빠른 시작

```bash
pip install -r requirements.txt
```

`.env.example`을 `.env`로 복사한 뒤 `OPENAI_API_KEY`를 채워요. `.env`는 저장소에 올라가지 않아요.

```bash
cp .env.example .env          # Windows 명령 프롬프트: copy .env.example .env
```

```bash
python agent.py "택배사는 어디고 보통 며칠 걸려요?"     # 문의 하나를 처리
streamlit run app.py                                   # 데모 화면 (http://localhost:8501)
```

Windows에서 `streamlit` 명령을 못 찾으면 `python -m streamlit run app.py`로 실행하세요.

## 평가 실행

```bash
python evaluate.py --validate-judge     # 채점기를 모범 답안으로 먼저 검증
python evaluate.py --tag run            # 평가용 25건 측정 → results/run.json
python evaluate.py --rejudge run        # 저장된 결과를 현재 평가셋으로 다시 채점
```

측정에는 OpenAI API 호출이 들어가요. 25건 한 번에 20초 안팎이 걸려요.

## 모델 설정

기본값은 교안과 같은 등급이에요. 환경변수로 바꿀 수 있어요.

| 환경변수 | 기본값 | 용도 |
|---|---|---|
| `ROUTE_MODEL` | `gpt-5.6-luna` | 카테고리 분류 |
| `ANSWER_MODEL` | `gpt-5.6-terra` | 도구 선택 · 답변 · 이관 답변 |
| `JUDGE_MODEL` | `ANSWER_MODEL`과 같음 | 답변 채점 |
| `ANSWER_REASONING` | `none` | 교안 모델은 추론 모드와 도구 호출을 함께 못 써서 끈다. 다른 모델을 쓰면 빈 값으로 둔다 |

계정에서 위 모델이 열리지 않으면 다른 모델명을 지정하세요. 점수는 모델에 따라 달라질 수 있어요.

## 구성

| 경로 | 역할 |
|---|---|
| [docs/policy.md](docs/policy.md) | 근거 문서(업무 매뉴얼 v0.2). 네이버 스마트스토어 판매 이용약관을 풀어 썼고, 약관에 없는 값은 `[가정]`, 근거를 못 찾은 것은 `[확인 필요]`로 표시 |
| [data/goldenset.json](data/goldenset.json) | 평가셋: 평가용 25 + 예시용 10 (기대 도구, 필수 사실, 금지 사항, 모범 답안) |
| [prompts.py](prompts.py) | 분류 지침 · 답변 규칙 · 이관 답변 규칙 |
| [context.py](context.py) | 문서를 장 단위로 쪼개고 도구별 근거를 조립 |
| [agent.py](agent.py) | LangGraph 파이프라인 · 도구 · 숫자 출처 가드레일 |
| [evaluate.py](evaluate.py) | 두 지표 측정 · 채점기 검증 · 재채점 |
| [app.py](app.py) · [assets/style.css](assets/style.css) | Streamlit 데모와 디자인 토큰 |
| [results/](results/) | 측정 결과 JSON과 개선 기록([LOG.md](results/LOG.md)) |
| [REPORT.md](REPORT.md) | 설계 결정 · 측정 결과 · 구조도 · 회고 |

## 알아 둘 점

- **가상의 스토어예요.** 가격, 상품 사양, 택배사, 반품 배송비 같은 값 40건은 약관에 없어서 가정한 값이고, 문서에 `[가정]`으로 표시했어요. 실제 사업 정보는 들어 있지 않아요.
- **약관은 요약해서 재서술했어요.** 원문을 복사하지 않았어요. 출처는 문서 꼬리표(`[약관 제N조]`)와 부록 B에 있어요.
- **점수는 실행마다 1건 정도 흔들려요.** 평가셋이 25건이라서, 한두 건 차이는 효과로 보지 않았어요(자세한 한계는 REPORT 4.6).
- **데모는 실제 노드 상태를 보여 줘요.** 처리 단계 표시는 가짜 진행률이 아니라 그래프가 끝낸 노드를 반영해요.
