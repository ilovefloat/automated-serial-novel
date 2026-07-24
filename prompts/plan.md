다음 자료를 바탕으로 공개 본문을 쓰기 전 비공개 장면 계획 하나를 작성하라.
계획은 품질 제어에만 쓰이며 독자에게 공개되지 않는다.

## 이번 화 번호
{episode_number}

## 압축 상태와 장기 아크
{story_state}

## 최근 화 서사 지문
{recent_fingerprints}

## 현재 냉각 중인 소재
{active_cooldowns}

## 최근 연재분 요약
{recent_summaries}

## 서사 속도 권고
{pace_guidance}

## 재계획 피드백
{feedback}

## 계획 원칙

- 게시 단위와 서사 단위를 일치시키지 않는다.
- 종료되지 않은 장면과 즉각적 행동이 있으면 그 정확한 지점부터 이어간다.
- 한 장면을 한 화 안에 억지로 끝내지 않는다.
- 주요 아크 1개, 보조 아크 0~2개만 선택한다.
- 현재 속도와 최근 속도 권고를 참고하되 기계적으로 순환하지 않는다.
- 최근 5화와 같은 주된 문서 형식을 피하되 직접 연속 장면이면 기능을 바꾼다.
- 냉각 중인 소재는 직접 연속 사건이 아니면 사용하지 않는다.
- `motifs_used`에는 해당할 때만 `major_accident`, `company_direct_warning`,
  `reader_comments_focus`, `approval_document_error`,
  `ai_profanity_sanitization`, `memory_uncertainty`,
  `legal_liability_crisis`, `repeated_ending_phrase` ID를 사용한다.
- 최근 지문과 장소·인물·갈등이 같아도 서사 기능과 결과를 다르게 만든다.
- 새 인물과 장소를 한꺼번에 늘리지 않는다.
- 매 화 새 노동 사건, AI 수정 장면, 사회비판 결론, 반전을 의무 배치하지 않는다.
- 계획만 출력하며 본문 문장을 미리 쓰지 않는다.

응답은 지정된 JSON 스키마를 따르는 객체 하나여야 한다.
