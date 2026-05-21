# Confluence Chunking — Findings

## 요약

- 총 발견 **12건** (Critical 2, High 7, Medium 3, Low 0)
- 가장 시급한 3건:
  - **F-01** chunker.py 폴백 인코딩이 overlap을 무효화 (tiktoken 없으면 청크 일관성 깨짐)
  - **F-02** count_tokens 폴백(`chars/4`) vs `_make_codec` 폴백(`ord` 1:1) 단위 불일치 → `overlap_tokens`이 의도의 25%만 적용
  - **F-09** `_split_oversized`로 분할된 거대 섹션의 part[1..N]이 부모 흡수 컨텍스트를 받지 못함 → 그래프 추출 LLM이 컨텍스트 결손

## 발견 사항

### F-01 (CRITICAL): chunker.py 폴백 encode/decode가 overlap을 무력화

- **위치**: `src/context_loop/processor/chunker.py:393-403, 461-466`
- **현재 동작**:
  ```python
  def encode(s: str) -> list[int]:
      if enc is not None:
          return list(enc.encode(s))
      return list(range(len(s)))      # 폴백: 0..len-1 (의미 없는 id)

  def decode(tokens: list[int], fallback: str) -> str:
      if enc is not None:
          return enc.decode(tokens)
      return fallback                  # 폴백: 토큰 무시, fallback 그대로
  ```
  - flush 시 `current_tokens = overlap_tokens[:]`로 overlap 토큰 들고 새 청크 시작
  - 새 블록의 텍스트는 `current_text_parts`에만 들어가고, flush 시 `decode(current_tokens, "\n\n".join(current_text_parts))` 호출
  - 폴백 환경에서 decode는 토큰을 무시하고 fallback(text_parts join)을 그대로 반환 → **overlap 텍스트가 청크에 포함되지 않음**
- **문제**: tiktoken이 없는 환경에서 overlap이 사실상 작동 안 함. 청크 경계에서 양방향 컨텍스트 부재 → 검색에서 경계 근처 정보 누락
- **재현/근거**:
  - `pip uninstall tiktoken` 후 `chunk_text("# A\n\n" + "para. " * 200 + "\n\n# B\n\nshort", chunk_size=100, chunk_overlap=20)` 호출
  - 모든 청크가 fallback 텍스트(원본 그대로)로 채워지며 overlap 적용 흔적 없음
- **개선 방향**:
  - (1) 폴백을 `ord(c)` round-trip 방식(`extraction_unit._make_codec`과 동일)으로 통일 → overlap 정확
  - (2) 또는 폴백 시 chunk_size를 character 기준으로 환산 (chars = tokens × 4)하고 character 슬라이싱
  - 권장: (1) — `extraction_unit`과 일관됨, 코드 중복 제거 효과
- **영향 범위**: 임베딩 입력 텍스트가 변함 → 검색 품질 영향. 폴백 환경 전용 이슈지만 운영 안정성 큰 차이
- **심각도**: Critical | **공수**: S (≤30분)

---

### F-02 (CRITICAL): `count_tokens` 폴백과 `_make_codec` 폴백의 토큰 단위 불일치

- **위치**: `src/context_loop/processor/chunker.py:86-99` ↔ `src/context_loop/processor/extraction_unit.py:602-609`
- **현재 동작**:
  - `chunker.count_tokens`: 폴백 시 `len(text) // 4` (4 char ≈ 1 token)
  - `extraction_unit._make_codec`: 폴백 시 `[ord(c) for c in s]` (1 char = 1 token)
  - `_take_tail_tokens(text, n=200, encode, decode)`은 `encode(text)[-200:]` → 200 char만 가져옴
  - 그러나 같은 시점 `count_tokens(text)`는 `len(text)//4` 기준
  - **결과**: 사용자가 `overlap_tokens=200`을 설정해도 폴백 환경에서는 실제로는 50 토큰(count_tokens 기준) 만큼만 overlap됨
- **문제**: 동일 폴백 환경에서 두 함수가 다른 토큰 단위 → `overlap_tokens`/`target_tokens`/`max_tokens` 설정이 의도의 25%만 적용
- **재현/근거**:
  ```python
  text = "abcd" * 100  # 400 chars
  count_tokens(text)   # → 100 (fallback)
  enc, dec = _make_codec("x")  # 폴백 codec
  len(enc(text))       # → 400 (1:1)
  ```
- **개선 방향**:
  - 두 폴백을 동일 정책으로 통일 (F-01 해결과 함께 ord 1:1로)
  - `count_tokens`의 폴백을 `len(text)` (1:1)로 바꾸고 `_CHARS_PER_TOKEN` 의미 제거
  - 다만 영문 텍스트에서 chunk_size=512가 사실상 character 512가 되어 매우 작은 청크가 됨 → 다음 발견과 묶어 처리
- **영향 범위**: extraction_unit의 모든 토큰 가드 (target/max/overlap/min/lead_paragraph_max)
- **심각도**: Critical | **공수**: S

---

### F-03 (HIGH): 한국어/일본어/중국어에서 폴백 chunk_size 과대평가

- **위치**: `src/context_loop/processor/chunker.py:29, 99`
- **현재 동작**: `_CHARS_PER_TOKEN = 4`로 폴백 토큰 추정 — 영어 기준 (BPE는 한국어 1 char ≈ 1.5~2 토큰)
- **문제**: 폴백 환경에서 한국어 문서의 청크 토큰 수가 50%~75% 과소평가 → chunk_size=512가 실제 한국어로 ~1000~1500 토큰 → 임베딩 모델 입력 제한 초과 가능
- **재현/근거**: `count_tokens("한글한글" * 200)` 폴백 → `400 // 4 = 100`. tiktoken 실측은 ≈ 400~500 tokens
- **개선 방향**:
  - 폴백 자체를 안전한 보수 기준으로 변경: `len(text) // 2` (CJK 안전)
  - 또는 CJK 비율 감지하여 동적 분모
  - 또는 F-01/F-02와 합쳐서 폴백을 ord 1:1로 두고 chunk_size를 character로 해석 (가장 단순)
- **영향 범위**: 폴백 환경의 모든 청크. 임베딩 모델 input length 초과로 잘리거나 에러
- **심각도**: High | **공수**: S

---

### F-04 (HIGH): `_chunk_blocks`의 overlap이 토큰 단위로 잘려 문장/단어 경계 깨짐

- **위치**: `src/context_loop/processor/chunker.py:430-431, 461-462`
- **현재 동작**:
  ```python
  overlap = current_tokens[-chunk_overlap:] if chunk_overlap else []
  flush(overlap)
  ```
  - tiktoken 토큰 단위로 마지막 N개를 가져와 다음 청크 시작에 prepend
  - 토큰 경계 ≠ 문장/단어 경계 → 디코드 결과가 부자연스러운 위치에서 시작
- **문제**: 임베딩 입력 텍스트가 "...ence end. New sent" 같이 단어 중간에서 시작 → 임베딩 품질 저하 + LLM 후처리(LLM body 추출 등)에서 컨텍스트 단편화
- **재현/근거**: `chunk_text(긴 한글 본문, chunk_size=100, chunk_overlap=20)` → 두 번째 청크의 시작이 어절 중간
- **개선 방향**:
  - (1) overlap을 블록 단위로: 이전 청크의 마지막 1~N개 블록 통째로 prepend (블록 = 문단/줄)
  - (2) overlap 토큰 위치를 가장 가까운 공백/문장부호로 스냅
  - 권장: (1) — `_split_markdown_blocks`가 이미 블록 단위라 활용 가능
- **영향 범위**: 모든 confluence_mcp 청크 (overlap > 0)
- **심각도**: High | **공수**: M (1~3h)

---

### F-05 (MEDIUM): atomic 블록(코드/표) 처리 후 overlap이 누락됨

- **위치**: `src/context_loop/processor/chunker.py:428-441`
- **현재 동작**: oversized atomic 블록(코드/표)이 단독 청크로 emit된 후, 다음 청크는 overlap 없이 시작 (이전 일반 블록의 overlap은 atomic emit 전 flush 시 적용되지만, atomic 자체에서 다음 청크로의 overlap은 X)
- **문제**: 표/코드 직후 텍스트 청크가 컨텍스트 결손 (어떤 코드 다음 설명인지 모름)
- **개선 방향**:
  - atomic 블록 emit 직후 다음 일반 블록 청크의 시작에 atomic 블록의 마지막 N토큰 (또는 첫 N줄) prepend
  - 또는 atomic 블록 다음 청크에 atomic의 lead context (예: 코드 첫 줄 + 표 헤더)를 prepend
- **영향 범위**: 코드/표 직후 본문 청크
- **심각도**: Medium | **공수**: M

---

### F-06 (HIGH): `chunk_text` 첫 헤딩 이전 텍스트의 section_path가 빈 문자열

- **위치**: `src/context_loop/processor/chunker.py:142-147`
- **현재 동작**:
  ```python
  pre_text = text[: headings[0][0]].strip()
  if pre_text:
      sections.append(_Section(heading_level=0, heading_text="", content=pre_text, path=[]))
  ```
  - path=[] → 후속 `_chunk_blocks` 결과의 `section_path=""`
- **문제**: 문서 도입부/요약/메타 설명이 위치 정보 없이 인덱싱 → 검색 결과 표시에서 "어느 문서의 어디" 식별 불가. meta-view 임베딩에서도 `build_meta_view_text(title, "")`이 title만 → 정보 약화
- **개선 방향**:
  - path=["(intro)"] 등 placeholder, 또는 별도 표시 "(도입부)"
  - `chunk_extracted_document`와 일관성 (그쪽은 `section.title`을 폴백 — F-07 참조)
- **영향 범위**: 헤딩 없는 부분이 있는 모든 문서. confluence_mcp는 보통 헤딩 풍부, upload/manual은 영향 큼
- **심각도**: High | **공수**: S

---

### F-07 (MEDIUM): `chunk_text`와 `chunk_extracted_document`의 section_path 폴백 정책 불일치

- **위치**: `src/context_loop/processor/chunker.py:304, 359`
- **현재 동작**:
  - `chunk_text` (line 304): `section_path = " > ".join(section.path) if section.path else ""`
  - `chunk_extracted_document` (line 359): `section_path = " > ".join(section.path) if section.path else section.title`
- **문제**: 같은 의도("section_path를 채워라")인데 빈 path일 때 다른 폴백 → confluence_extractor 경로와 일반 텍스트 경로의 검색 결과 표시가 일관되지 않음
- **개선 방향**: 두 함수 모두 동일 정책 사용 (예: title이 있으면 title, 없으면 "(intro)")
- **영향 범위**: confluence_mcp는 일반적으로 sections이 있으므로 영향 적음. 그러나 일관성 부족이 향후 버그 양산
- **심각도**: Medium | **공수**: S

---

### F-08 (HIGH): `_split_oversized`의 첫 블록이 단독 part여도 max_tokens 초과 가능

- **위치**: `src/context_loop/processor/extraction_unit.py:430-441`
- **현재 동작**:
  ```python
  for block in blocks:
      b_tokens = count_tokens(block.content, cfg.encoding_model)
      if parts[-1] and current_tokens + b_tokens > cfg.target_tokens:
          parts.append([])
          current_tokens = 0
      parts[-1].append(block)
      current_tokens += b_tokens
  ```
  - 단일 atomic 블록(거대 코드/표)이 target_tokens 초과해도 단독 part로 들어감
  - max_tokens(2400) 초과해도 가드 없음 → 후속 LLM body 추출에서 context 초과 위험
- **문제**: 한 part가 LLM input 한계(예: 4k context의 모델)를 초과하면 LLM 호출 실패 (현재 stats.units_failed에 반영)
- **개선 방향**:
  - 단일 블록이 max_tokens 초과 시 경고 로깅 + atomic 블록도 강제 분할 옵션 (config flag)
  - 또는 LLM 호출 전 unit.token_count 사전 검사하여 초과 unit은 스킵 (이미 stats만 있고 가드 없음)
- **영향 범위**: 거대 표/코드를 포함한 문서. LLM body 추출 실패율
- **심각도**: High | **공수**: S

---

### F-09 (HIGH): 부모 own 흡수가 분할된 첫 자식의 part[0]만 적용 → part[1..N]은 컨텍스트 결손

- **위치**: `src/context_loop/processor/extraction_unit.py:343-363`
- **현재 동작**:
  ```python
  for child in node.children:
      child_units = _collect_units(child, ...)
      if absorb_pending and child_units:
          parent_node, parent_body = absorb_pending
          first = child_units[0]
          new_body = parent_body + "\n\n" + first.body
          ...
          child_units[0] = _PreUnit(..., body=new_body, ...)
          absorb_pending = None
      units.extend(child_units)
  ```
  - 첫 자식만 absorb_pending 받음 + 자식이 split됐다면 split_part[0]만 받음
- **문제**: 부모 헤딩이 짧고 자식이 거대하여 split되면, split_part[1..N]은 부모 컨텍스트 없음 → LLM 추출에서 "이 part가 어느 부모의 분할인지" 모름. (현재 breadcrumb에 section_path는 있으나 부모 본문의 키워드/약어 정의는 없음)
- **개선 방향**:
  - 부모 own_body 일부(예: 첫 문단 / lead)를 split된 모든 part의 breadcrumb에 lead_paragraph로 prepend
  - 또는 부모 흡수를 한 번이 아니라 모든 자식의 첫 part에 적용
- **영향 범위**: 거대 섹션이 짧은 부모 아래에 있는 문서. 그래프 추출 LLM의 정확도
- **심각도**: High | **공수**: M

---

### F-10 (MEDIUM): `_extract_lead_paragraph`이 plain_text에서 마크다운 헤딩을 찾음 → 변환 누락 위험

- **위치**: `src/context_loop/processor/extraction_unit.py:578-585`
- **현재 동작**: `_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)`로 plain_text의 첫 헤딩을 찾고 그 이전을 lead로
- **문제**: plain_text는 `html_to_markdown` 결과인데, 그 결과에 마크다운 `#` 헤딩이 보장되지 않음 (변환기에 따라 다름). 결과적으로 lead가 잘못 잡힘 (헤딩이 없으면 전체 plain_text가 lead가 됨 — 거대 lead)
- **개선 방향**:
  - `extracted.sections`이 있으면 첫 section.title을 plain_text에서 검색하여 그 이전을 lead로
  - 또는 sections[0].md_content 이전 영역을 별도로 추출하여 보관
- **영향 범위**: 모든 confluence_mcp 문서의 breadcrumb (lead_paragraph)
- **심각도**: Medium | **공수**: S

---

### F-11 (MEDIUM): confluence_extractor의 동일 페이지 OutLink가 중복 생성, label은 첫 등장만 보존

- **위치**: `src/context_loop/ingestion/confluence_extractor.py:242-292` + `src/context_loop/processor/link_graph_builder.py:84-110`
- **현재 동작**: 같은 페이지 링크가 본문에 N번 나오면 N개 OutLink. link_graph_builder가 `(source, target, relation_type)` 3-튜플로 dedup하지만 첫 등장의 in_section만 label에 저장
- **문제**: 동일 페이지가 여러 섹션에서 참조되면 "어디서 참조" 정보가 첫 섹션으로만 좁혀짐 → 검색 결과 추적성 손실
- **개선 방향**:
  - extractor에서 (target_id, target_title, kind) 기준 dedup하면서 in_sections를 list로 누적
  - 또는 link_graph_builder에서 dedup 시 in_section 누적
- **영향 범위**: 본문 그래프의 라벨 정보 — 검색 결과 출처 표시
- **심각도**: Medium | **공수**: S

---

### F-12 (HIGH): `_section_body_markdown`이 next_siblings만 따라가 nested 헤딩 본문 누락

- **위치**: `src/context_loop/ingestion/confluence_extractor.py:206-219`
- **현재 동작**:
  ```python
  for sibling in heading.next_siblings:
      if isinstance(sibling, Tag) and sibling.name in _HEADING_TAGS:
          sibling_level = int(sibling.name[1])
          if sibling_level <= level:
              break
      parts.append(str(sibling))
  ```
  - 동일 부모의 형제 노드만 수집
- **문제**: Confluence가 `<ac:structured-macro ac:name="expand">` / `<div class="conf-macro">` 안에 헤딩을 감싸면, 그 헤딩의 본문(같은 매크로 내부의 후속 sibling)은 추출되지만, 매크로 외부의 후속 본문은 누락. 더 흔한 케이스: 헤딩이 `<div>` 안에 있고 본문이 `<div>` 밖에 있을 때 누락
- **재현/근거**: Confluence "Information"/"Expand" 매크로 안의 H2/H3 → H2 본문이 expand 내용만, H3는 expand 안에서 매핑돼야 하지만 sibling 한정으로 일부 누락
- **개선 방향**:
  - DOM 전체 walk로 헤딩 사이의 모든 노드를 수집 (heading의 next_in_document_order 사용, 다음 같은/상위 레벨 헤딩까지)
  - 또는 BeautifulSoup의 `find_all_next` 사용
- **영향 범위**: Expand/Info 매크로를 활용한 confluence 문서의 섹션 본문
- **심각도**: High | **공수**: M

---

## 검토하지 않은 영역

- `html_converter.py` 의 마크다운 변환 충실도 (별도 분석 필요)
- Confluence 매크로 (info/warning/panel/expand/status) 각각의 변환 결과
- `mcp_confluence.py` 의 페이지 가져오기 정확성 (인덱싱이 아니라 수집 단계)
- chunk_id의 안정성 (재처리 시 신규 uuid 발급되는데, 검색 결과 캐시/즐겨찾기 영향 미검토)
