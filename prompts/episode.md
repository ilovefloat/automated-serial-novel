다음 자료와 비공개 장면 계획을 바탕으로 연속 장편소설의 다음 게시분을 작성하라.

## 이번 화 번호
{episode_number}

## 기존 진행 상태
{story_state}

## 고정 설정과 연속성 메모
{continuity}

## 최근 연재분
{recent_episodes}

## 비공개 장면 계획
{scene_plan}

## 작성 원칙

1. `# 제목` 다음에 본문을 쓴다. 제목에는 화수를 넣지 않는다.
2. 기본 분량은 한국어 1,500~4,500자다. 짧고 강한 장면은 1,000자 전후,
   중요한 대화나 사건은 5,000자 이상도 허용한다.
3. 분량을 채우려고 내면 독백과 사회비판을 반복하지 않고, 분량을 줄이려고
   사건과 감정을 요약하지 않는다. 긴 장면은 다음 화로 자연스럽게 이어간다.
4. 계획의 직접 연결 지점과 미완료 행동을 지킨다. 이전 장면이 진행 중이면
   새 도입부나 시간 요약으로 우회하지 않는다.
5. 매 화마다 새 노동 사건, AI 수정 갈등, 교훈, 반전을 하나씩 넣지 않는다.
6. 같은 장소나 반복 노동을 다시 쓰면 이전과 다른 서사 기능을 갖게 한다.
7. AI를 악당으로 만들지 않고 수익성, 책임 전가, 비용 구조를 구체적 사건으로
   드러낸다.
8. 작품 속 원고와 독자가 읽는 본문의 경계는 필요할 때만 흐린다.
9. 계획에 없는 장기 비밀을 성급하게 공개하지 않는다.
10. 끝에는 정서적 변화, 정보, 결정, 질문, 이미지 중 하나를 남기되 강한
    절단이나 짧은 격언을 의무적으로 만들지 않는다.
11. 전체 응답과 공개 본문을 코드펜스로 감싸지 않는다.

## 출력 형식

응답은 아래 구조의 JSON 객체 하나여야 한다. 공개 본문과 비공개 상태 갱신을
섞지 않는다.

{{
  "public_markdown": "# 제목\n\n공개할 소설 본문",
  "state_update": {{
    "summary": "이번 게시분에서 실제로 진행된 내용의 압축 요약",
    "protagonist_changes": ["주인공 상태의 실제 변화"],
    "new_facts": ["새로 확정된 사실"],
    "open_threads": ["현재 남아 있는 복선과 갈등"],
    "continuity_notes": ["다음 집필에서 지켜야 할 연속성"],
    "next_episode_pressure": "다음 게시분으로 이어지는 현실적 압력",
    "actual_time_range": "본문 내부에서 실제로 흐른 시간",
    "direct_continuation": true,
    "scene_completed": false,
    "current_scene": "현재 장면",
    "next_immediate_actions": ["다음 화 첫 부분에서 이어야 하는 행동"],
    "primary_arc_progress": {{
      "arc": "등록된 주요 아크 이름",
      "stage": "현재 단계",
      "change": "이번 화에서 바뀐 것",
      "next_possible_change": "다음 가능한 변화",
      "withheld_information": ["이 아크에서 아직 공개하면 안 되는 정보"],
      "current_pressure": "현재 압력",
      "long_term_outcome": "장기적으로 가능한 결과",
      "on_hold": false
    }},
    "supporting_arc_progress": [],
    "revealed_information": ["새로 공개한 정보"],
    "withheld_information": ["의도적으로 보류한 정보"],
    "planned_long_term_reveals": ["후반 공개를 위해 유지할 정보"],
    "locations": ["사용한 장소"],
    "character_combination": ["함께 등장한 인물"],
    "central_labor": "중심 노동 또는 없음",
    "conflict_types": ["갈등 유형"],
    "social_themes": ["사회적 쟁점"],
    "document_formats": ["사용한 문서 형식 또는 일반 서술"],
    "ai_interaction_types": ["AI 상호작용 방식 또는 없음"],
    "emotional_start": "감정의 시작점",
    "emotional_end": "감정의 종착점",
    "opening_pattern": "화의 시작 방식",
    "ending_pattern": "화의 종료 방식",
    "core_images": ["핵심 이미지"],
    "core_sentence_structure": "두드러진 문장 구조",
    "repetition_risks": ["반복 위험 요소"],
    "variations_applied": ["적용한 변주"],
    "motif_cooldown_updates": ["계획의 motifs_used 중 실제로 강하게 사용한 ID"],
    "new_threads": ["새 복선"],
    "maintained_threads": ["유지 복선"],
    "resolved_threads": ["회수 복선"],
    "new_questions": ["새로 제기된 질문"],
    "next_required_connection": "다음 화의 필수 연결점",
    "narrative_mode": "이번 화의 주된 서술 형식",
    "narrative_pace": "정체|일상 축적|관계 심화|위험 증가|사건 전개|여파|회복|방향 전환",
    "narrative_function": "같은 소재가 이번 화에서 수행한 서사 기능",
    "twist_type": "사용한 반전 유형 또는 없음",
    "major_event": false,
    "major_reveal": false,
    "symbol_updates": [
      {{
        "symbol": "반복 상징",
        "meaning": "이번 화에서의 의미",
        "development": "이전 의미와 달라진 점"
      }}
    ]
  }}
}}
