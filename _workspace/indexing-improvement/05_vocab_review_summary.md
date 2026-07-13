# 05 · graph_vocabulary.py 어휘 제한 적정성 검토 — 종합 (검토 전용)

- 라운드: 2026-07-13 · graph_vocabulary 검토 (구현 없음)
- 입력: `03_confluence_graph_findings.md` (F-CG-01~05), `04_git_code_graph_findings.md` (F-GG-01~08)
- 대상: `src/context_loop/processor/graph_vocabulary.py` — 15 entity / 18 relation 화이트리스트

## 결론 (한 문단)

어휘 정의 자체는 현행 추출기들이 실제 방출하는 타입과 **표면적으로는 일치**한다
(미선언 방출/미방출 선언 없음). 그러나 (1) 이 모듈의 존재 이유 절반인 **검색 측
정렬이 미실현** — `graph_search.py` 는 vocab 을 import 하지 않으며
`INTENT_TO_RELATIONS` 와 포매터·`all_*` 헬퍼는 프로덕션 소비처 0 (데드코드,
docstring 은 삭제된 LLM 플래너를 전제), (2) **relation 폭이 실질 질의 요구 대비
과소** — git_code 그래프는 `imports`/`contains` 2종뿐이라 호출/상속/구현 질의가
불가하고, confluence subset 은 `depends_on/uses/calls` 3분산과 `concept` sink
편중, (3) **"테스트가 누락을 잡는다"는 보장이 미성립** — llm_body 가드는 순환이라
항상 통과, ast/body 가드는 하드코딩 미러. 즉 "제한 자체"보다 **제한을 지탱하는
소비처·가드·의미 해상도**가 문제다.

## 심각도별 통합 발견

### HIGH
- **F-GG-05**: AST relation 은 `imports`/`contains` 2종뿐. `calls`/`implements`/
  `depends_on` 은 llm_body 전용 선언이라 코드 그래프에 존재 불가 → 호출/상속/구현
  질의 응답 불가.
- **F-GG-06 / F-CG-04**: 검색 경로가 vocab 미소비. `INTENT_TO_RELATIONS`·전체
  포매터·`all_*_type_names` 는 테스트 전용 데드코드. docstring(graph_vocabulary.py:4-7,
  158-162)은 존재하지 않는 검색 LLM 플래너를 전제. INTENT 미커버 relation =
  has_attachment/has_attribute/imports/contains.

### MED
- **F-CG-01 / F-GG-02**: 상위집합 가드 테스트 무력 — llm_body 는 vocab 파생값
  비교(순환), body/ast 는 하드코딩 상수 대조로 실제 `to_graph_data` 출력 드리프트를
  못 잡음. 실효 가드는 link_graph 뿐.
- **F-CG-02**: `_has_source` substring 필터는 현행 안전하나, 향후 `"body"`/`"ast"`
  keyword subset 추가 시 역방향 오검출(`"body" in "llm_body + ast_code"` → True).
- **F-CG-03**: confluence LLM 실효 어휘 = 7 entity / 9 relation. datastore·event·
  environment·config 축 결손 → `concept` sink 남용. `depends_on/uses/calls`,
  `documents/documented_in`, `module/system`, `has_part/contains` 중복·모호.
- **F-GG-03**: `module` 과부하 — 파일/import 모듈/`__module__`/`__constants__`
  4종 겸용(실측). `constant`/`variable`/`type_alias` 어휘 부재.
- **F-GG-04**: Java `interface`/`enum` 이 모두 `class` 로 emit(실측) → 어휘의
  "interface(Java)" 설명 부정확, `enum` 어휘 부재, TS enum 미추출.
- **F-GG-07**: `module` 이중 소속(llm_body+ast_code)은 명명 규칙 상이(`.py`/dotted
  vs 산문명, 정규화가 `.` 보존)로 실효 병합 거의 없음 — 명목상 union. 드문 동명
  오병합 위험만 잔존(F-CG-03 의 doc↔code 오병합 우려와 동일 지점).

### LOW
- **F-GG-01**: coarse 정합 성립(확인).
- **F-GG-08**: 크로스파일 Go struct parent 가 `class` 로 강제되는 엣지케이스.
- **F-CG-05 / F-GG(6)**: 로컬 graph_store 산출물 부재로 실분포 대조 생략
  (git_code 는 추출기 실행 실측으로 대체).

## 권고 방향 (구현은 다음 라운드에서 의사결정 후)

**R1 후보 (구조 정합 — 어휘 폭 변경 없이 가능)**
1. 검색 정렬 방향 의사결정: (a) vocab 소비 재배선(임베딩 시딩 + relation 가이드/
   부스팅) 또는 (b) 검색용 헬퍼·INTENT·docstring 을 데드코드로 정리. 어느 쪽이든
   docstring 의 스테일 전제 갱신. [F-CG-04/F-GG-06]
2. 가드 테스트를 실제 방출 기반으로 재설계(추출기 `EMITTED_*` 상수 노출 또는
   소형 픽스처 실행 대조). [F-CG-01/F-GG-02]
3. `source` 를 태그 집합으로 모델링(또는 토큰 단위 매칭)해 substring 함정 제거.
   [F-CG-02]
4. 어휘 설명 정확화: interface 설명에서 Java 제외(또는 추출기 세분화 전까지 주석),
   module 의 실제 포괄 범위 명시, "module 이중소속은 명목상" 주석. [F-GG-04/03/07]

**R2 후보 (어휘 폭·의미 변경 — 실데이터 분포 확인 후)**
5. AST relation 확장(extends/implements 를 시그니처에서 도출, calls 는 비용/정확도
   트레이드오프 평가). [F-GG-05]
6. 의존류(depends_on/uses/calls) 수렴 또는 사용 규칙 명문화,
   documents/documented_in 통일. [F-CG-03]
7. entity 축 보강(datastore/event 신중 추가, enum/constant 신설) + module 의
   코드 파일 의미 분리. [F-CG-03/F-GG-03/04]

**전제**: 어휘 폭 조정(R2)은 실제 graph_store 분포 확인 후 진행. 평가/골드셋
정합(`eval/*`)은 별도 하네스 영역 — type 통폐합 시 그쪽 하네스에서 별도 확인 필요.
