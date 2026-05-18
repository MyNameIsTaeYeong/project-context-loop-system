# 사용자 요구사항 기준선 (2차 — 그래프 인덱싱 강건성)

**일시**: 2026-05-18 (2차)
**이전 작업**: `_workspace_prev_20260518_105045/` — chunk-size 강건 + chunk/graph 평가 도입은 이미 완료됨.
**대상**: `src/context_loop/eval/gold_set.py`(`GraphEntityRef`), `src/context_loop/eval/synth.py`(graph 질문 생성), `scripts/eval_search.py`(평가), `scripts/build_synthetic_gold_set.py`(graph 모드).

## 배경 — 이전 작업의 한계

이전 작업으로 청크 사이즈 변경에는 강건해졌지만, **그래프 인덱싱 로직 변경**에는 여전히 취약하다는 점이 분석에서 드러났다:

- 현재 매칭 키 = `(entity_name.lower(), entity_type)` exact match (`eval_search.py:190-196`)
- entity_type 어휘가 바뀌면 (`system` → `service`) 매칭 실패 → graph_* 메트릭 인공 하락
- name 정규화 로직 변경 시에도 매칭 실패
- 엔티티 병합·canonical 처리·alias 미지원

`metrics_by_mode` split 덕분에 doc-level 채점은 보존되지만, graph-level 신호는 인덱싱 변경 시점에 손실된다. 골드셋의 그래프 평가 가치가 인덱싱 변경 한 번에 사라지는 것은 비싸다 — 골드셋 생성에 LLM 토큰이 든다.

## 만족해야 할 조건

### R1. 그래프 항목이 정확한 node_id 나 entity_name 형태에 비의존
- 골드셋의 그래프 정답이 특정 추출 어휘(예: type 명명, name 정규화 방식)에 묶이지 않아야 한다.
- 새 그래프가 같은 의미의 엔티티/관계를 다른 표기로 보유하더라도 매칭이 가능해야 한다.

### R2. 의미 기반 매칭 또는 텍스트 앵커 기반으로 새 그래프에 매핑 가능
- 다음 중 하나 이상 채택:
  - (a) **시멘틱 매칭** — 임베딩/LLM 으로 정답 entity 와 검색 entity 의 의미 일치 판정.
  - (b) **텍스트 앵커 기반** — 골드셋이 자연어 evidence ("결제 서비스가 주문 서비스에 의존한다") 를 보유하고, 평가 시 검색된 graph context 에 그 사실이 포함되는지 판정.
  - (c) **alias / canonical 매핑** — 골드셋이 여러 후보 표기를 보유하고 OR 매칭.
- 채택한 방식의 비용·정확도 트레이드오프를 설계에 명시.

### R3. entity 병합 / 관계 타입 변경 / 신규 entity 추출에 robust
- entity 병합으로 두 노드가 하나로 합쳐져도 매칭 가능.
- 관계 타입이 `depends_on` → `requires` 로 바뀌어도 (의미가 같다면) 매칭 가능.
- 새 entity 추출이 추가되어 검색 결과가 풍부해진 경우, **거짓 양성으로 인한 precision 하락이 과도하지 않아야** 한다 (recall 우선이 자연스럽지만 명시 필요).

## 비기능 요구사항

- **Backward compatibility**: 기존 골드셋 YAML (1차 작업으로 생성된 것) 이 새 코드에서 로드되어야 한다. 누락 필드는 graceful degradation.
- **LLM 비용 고려**: 의미 매칭이 매 평가마다 LLM 호출이라면 비용·지연이 폭주. 캐시·임베딩 기반·offline 정규화 등으로 비용 통제 방안 명시.
- **결정론**: 같은 시드 + 같은 코퍼스 + 같은 LLM 결과면 같은 평가 점수가 나와야 한다 (재현성).
- **테스트**: LLM 호출은 mock. 기존 `tests/test_eval/` 통과 + 신규 동작 테스트 추가.
- **`metrics_by_mode` split 보고 유지**.

## 비목표 (Out of Scope)

- 그래프 인덱싱 자체의 변경/개선 (이 작업은 평가 시스템 한정).
- chunk 모드 채점 로직 변경 (이미 anchor 기반).
- 다른 source_type (upload/manual) 의 graph 평가 — 기존 범위 유지.
- 평가 메트릭의 종류 변경 (recall/precision/mrr/ndcg 그대로 유지, 매칭 키만 강건화).

## 핵심 의사결정 후보 (designer 결정)

analyst 분석 후 designer 가 결정해야 할 사항을 미리 메모:

1. **시멘틱 매칭 채택 여부** — 채택 시 임베딩 vs LLM, 비용·결정성 트레이드오프
2. **골드셋 스키마 변경 범위** — `GraphEntityRef` 에 `aliases`, `description`, `relations` 등 추가 vs 새 데이터 클래스 (`GraphFact`, `GraphEvidence`) 도입
3. **관계(엣지) 평가의 위상** — 현재 평가는 노드 hit 단위. 관계 타입 변경에 robust하려면 관계도 채점 단위에 포함시키는 게 자연스러우나 복잡도 ↑
4. **매칭 정책**: strict (exact) 와 fuzzy (semantic) 을 모드 옵션으로 분리할지, 하나로 통합할지
5. **재합성 도구 제공 여부** — 인덱싱 변경 후 골드셋의 entity_type/alias 만 일괄 갱신해주는 헬퍼

## 산출물

1. `_workspace/01_analysis.md` (analyst — 현재 매칭 로직 깊이 분석, 시나리오별 깨짐 패턴)
2. `_workspace/02_design.md` (designer — 위 5개 결정 + 새 스키마 + 평가 로직 + 비용 분석)
3. `_workspace/03_implementation.md` + 실제 코드 변경 (implementer)
