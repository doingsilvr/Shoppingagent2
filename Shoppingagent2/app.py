import re
import streamlit as st
import time
import html
import json
from openai import OpenAI
from google.oauth2.service_account import Credentials
import gspread
import uuid

# ======================================================
# 0) Google Sheets 인증 (Secret 기반)
# ======================================================
def get_gsheet_client():
    """
    Streamlit Cloud에서 JSON 파일 없이 인증하는 함수
    secrets.toml → [gcp_service_account] 블록 사용
    """

    service_json = st.secrets["gcp_service_account"]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        dict(service_json),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
    )
    return gspread.authorize(creds)

# ======================================================
# 1) 이벤트 단위 로그 기록 (A_raw) — 최종 안정 버전
# ======================================================
def log_event(event_type, **kwargs):
    """
    A_raw 시트에 이벤트 단위 로그 한 줄을 기록.
    - event_type: 이벤트 종류 (user_message / memory_add / memory_delete ...)
    - kwargs:
        source="user" | "agent"
        text, value, new_value, old_value, index, memory_count 등
    """

    # --------------------------------------------------
    # 1) 한 이벤트(entry) 구성
    # --------------------------------------------------
    entry = {
        "timestamp": time.time(),
        "session_id": st.session_state.get("session_id", "unknown"),
        "condition": "A",
        "user_name": st.session_state.get("nickname", ""),
        "phase": st.session_state.get("stage", "unknown"),
        "event_type": event_type,

        # 🔥 추가: 사람/AI 구분
        "source": kwargs.get("source", "agent"),

        # 🔥 선택적 값들
        "text": kwargs.get("text", ""),
        "value": kwargs.get("value", ""),
        "new_value": kwargs.get("new_value", ""),
        "old_value": kwargs.get("old_value", ""),
        "index": kwargs.get("index", ""),
        "memory_count": kwargs.get("memory_count", ""),
    }

    # --------------------------------------------------
    # 2) 세션 내 메모리에도 저장 (종료 후 summary용)
    # --------------------------------------------------
    st.session_state.logs.append(entry)

    # --------------------------------------------------
    # 3) Google Sheet에 한 줄 전송
    # --------------------------------------------------
    row = list(entry.values())  # 컬럼 순서 그대로 전송

    try:
        client = get_gsheet_client()
        sheet = client.open("shopping_logs").worksheet("A_raw")
        sheet.append_row(row, value_input_option="RAW")

    except Exception as e:
        print("Logging Error:", e)


# ======================================================
# 2) 세션 요약 기록 함수 (최종)
# ======================================================
def write_session_summary():

    ss = st.session_state
    logs = ss.logs

    if not logs:
        return False  # summary 기록 안 했음

    # ---- TURN COUNTS ----
    total_turns = sum(
        1 for e in logs if e["event_type"] in ["user_message", "assistant_message"]
    )
    explore_turns = sum(1 for e in logs if e["phase"] == "explore" and e["event_type"] == "user_message")
    summary_turns = sum(1 for e in logs if e["phase"] == "summary" and e["event_type"] == "user_message")
    compare_turns = sum(1 for e in logs if e["phase"] == "comparison" and e["event_type"] == "user_message")
    detail_turns = sum(1 for e in logs if e["phase"] == "product_detail" and e["event_type"] == "user_message")

    # ---- MEMORY EDIT COUNTS (전체) ----
    mem_add = sum(1 for e in logs if e["event_type"] == "memory_add")
    mem_delete = sum(1 for e in logs if e["event_type"] == "memory_delete")
    mem_update = sum(1 for e in logs if e["event_type"] == "memory_update")
    mem_edit_total = mem_add + mem_delete + mem_update

    # ---- USER-ONLY EDIT COUNTS (버튼 누른 것) ----
    user_add_count = sum(1 for e in logs if e["event_type"] == "memory_add" and e.get("source") == "user")
    user_delete_count = sum(1 for e in logs if e["event_type"] == "memory_delete" and e.get("source") == "user")

    # ---- HUMAN TOTAL ----
    human_edit_total = user_add_count + user_delete_count

    # ---- TIME ----
    timestamps = [e["timestamp"] for e in logs]
    total_duration = max(timestamps) - min(timestamps) if timestamps else 0

    # ---- FINAL CHOICE ----
    final_choice_evt = next((e for e in logs if e["event_type"] == "final_decision"), None)
    final_choice = final_choice_evt["value"] if final_choice_evt else ""

    # ---- DECISION TIME ----
    reco_evt = next((e for e in logs if e["event_type"] == "show_candidates"), None)
    decision_time = final_choice_evt["timestamp"] - reco_evt["timestamp"] if reco_evt and final_choice_evt else ""

    # ---- 최종 저장될 row ----
    summary_row = [
        ss.session_id,
        ss.nickname,
        ss.phone_number,
        ss.primary_style,
        total_turns,
        explore_turns,
        summary_turns,
        compare_turns,
        detail_turns,
        mem_add,
        mem_delete,
        mem_update,
        mem_edit_total,
        user_add_count,
        user_delete_count,
        human_edit_total,  # 🔥 휴먼 총편집
        total_duration,
        final_choice,
        decision_time,
    ]

    try:
        gs = get_gsheet_client()
        sheet = gs.open("shopping_logs").worksheet("session_summary")
        sheet.append_row(summary_row, value_input_option="RAW")
        return True

    except Exception as e:
        print("Summary Error:", e)
        return False

# =========================================================
# 0. 기본 설정
# =========================================================
st.set_page_config(
    page_title="AI 쇼핑 에이전트",
    page_icon="🎧",
    layout="wide"
)

client = OpenAI()

# =========================================================
# 1. 세션 상태 초기값 설정
# =========================================================
def ss_init():
    ss = st.session_state

    # 기본 UI 상태
    ss.setdefault("page", "context_setting")
    ss.setdefault("nickname", "")
    ss.setdefault("budget", None)

    # 대화 메시지 / 메모리
    ss.setdefault("messages", [])
    ss.setdefault("memory", [])
    ss.setdefault("just_updated_memory", False)


    # 단계
    ss.setdefault("stage", "explore")
    ss.setdefault("summary_text", "")
    ss.setdefault("detail_mode", False)

    # 추천 관련
    ss.setdefault("current_recommendation", [])
    ss.setdefault("selected_product", None)
    ss.setdefault("final_choice", None)

    # 로그용
    ss.setdefault("turn_count", 0)
    ss.setdefault("logs", [])
    ss.setdefault("session_id", str(uuid.uuid4()))
    ss.setdefault("condition", "A")  # 나중에 B로 변경 가능
    ss.setdefault("summary_written", False)

    # 🔥 추가된 핵심 상태값들 — 여기부터 추가
    ss.setdefault("question_history", [])           # 이미 어떤 질문을 했는지 추적
    ss.setdefault("current_question", None)         # 현재 진행 중인 질문 ID
    ss.setdefault("priority", "")                   # 실험 준비 단계에서 받아오는 최우선 기준
    ss.setdefault("neg_responses", [
        "없어", "몰라", "글쎄", "아니", "별로", "중요하지 않아",
        "그만", "그닥", "대충", "흠", "모르겠", "별로", "괜찮"
    ])


ss_init()

# =========================================================
# 글로벌 상수 정의
# =========================================================

YES_KEYWORDS = [
    "응", "응응", "ㅇㅇ", "네", "넹", "맞아", "필요해", "맞아요",
    "그래", "좋아", "좋아요", "중요하지", "좋지", "그치", "맞지"
]

NO_KEYWORDS = [
    "아니", "아니요", "별로", "그닥",
    "상관없어", "흠", "몰라", "않아", "없어"
]
# ========================================================
# 2. CSS 스타일 (기존 UI 완벽 유지)
# =========================================================
st.markdown("""
<style>
    /* 전체 UI 15% 축소 효과 */
    html, body, [class*="block-container"] {
        font-size: 0.85rem !important; /* 기본 폰트 약 -15% */
    }
    
    .chat-display-area {
        transform: scale(0.92);
        transform-origin: top left;
    }
    
    .product-card, .memory-block {
        transform: scale(0.95);
        transform-origin: top left;
    }
    /* 기본 설정 */
    #MainMenu, footer, header, .css-1r6q61a {visibility: hidden; display: none !important;}
    .block-container {padding-top: 1.5rem; max-width: 900px !important; padding-left: 1.5rem !important;padding-right: 1.5rem ! important;}

    /* 🔵 [버튼 스타일] 파란색(#2563EB) 통일 */
    div.stButton > button {
        margin-top: 0 !important;
        margin-bottom: 0 !important;
    }
        background-color: #2563EB !important;
        color: white !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        transition: background-color 0.2s ease;
    }
    div.stButton > button:hover {
        background-color: #1D4ED8 !important;
    }
    
    /* 🔵 [메모리 삭제 버튼(X)] 예외 스타일 */
    div[data-testid="stBlinkContainer"] button {
        background-color: #ffffff !important;
        color: #2563EB !important;
        border: 1px solid #E5E7EB !important;
        padding: 2px 8px !important;
        min-height: 0px !important;
        height: auto !important;
        margin: 0 !important;
    }
    div[data-testid="stBlinkContainer"] button:hover {
        background-color: #EFF6FF !important;
        border-color: #2563EB !important;
    }

    /* 🟢 진행바 (가로 배열 + 설명 포함) */
    .progress-container {
        display: flex; justify-content: space-between; margin-bottom: 30px;
        padding: 0 10px; gap: 20px;
    }
    .step-item {
        display: flex; 
        flex-direction: column; 
        align-items: flex-start; 
        flex: 1; 
        position: relative;
    }
    .step-header-group { 
        display: flex; 
        align-items: center; 
        margin-bottom: 6px; 
    }
    .step-circle {
        width: 28px; height: 28px; border-radius: 50%; background: #E5E7EB;
        color: #6B7280; display: flex; align-items: center; justify-content: center;
        font-weight: 700; margin-right: 10px; font-size: 13px; flex-shrink: 0;
    }
    .step-title { 
        font-size: 16px; font-weight: 700; color: #374151; 
    }
    .step-desc { 
        font-size: 13px; color: #6B7280; 
        padding-left: 38px; 
        line-height: 1.4; 
        max-width: 90%;
    }
    .memory-section {
        background: #FFFFFF;
        border-radius: 16px;
        padding: 20px 24px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.06);
        margin-bottom: 22px;
        max-width: 480px;
        margin-left: auto;
        margin-right: auto;
    }

    /* 활성화된 단계 스타일 */
    .step-active .step-circle { background: #2563EB; color: white; }
    .step-active .step-title { color: #2563EB; }
    .step-active .step-desc { color: #4B5563; font-weight: 500; }

    /* 🟢 채팅창 스타일 */
    .chat-display-area {
        height: 450px; overflow-y: auto; padding: 20px; background: #FFFFFF;
        border: 1px solid #E5E7EB; border-radius: 16px; margin-bottom: 20px;
        display: flex; flex-direction: column;
    }
    .chat-bubble { padding: 12px 16px; border-radius: 16px; margin-bottom: 10px; max-width: 85%; line-height: 1.5; }
    .chat-bubble-user { background: #E0E7FF; align-self: flex-end; margin-left: auto; color: #111; border-top-right-radius: 2px; }
    .chat-bubble-ai { background: #F3F4F6; align-self: flex-start; margin-right: auto; color: #111; border-top-left-radius: 2px; }

    /* 좌측 메모리 패널 스타일 */
    .memory-section-header {
        font-size: 20px; font-weight: 800; margin-top: 0px; margin-bottom: 12px; color: #111; display: flex; align-items: center;
    }
    .memory-guide-box {
        background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px;
        padding: 12px; font-size: 13px; color: #64748B; margin-bottom: 15px;
        line-height: 1.4;
     }       
    .memory-block {
        background: #FFF9D9;  /* 파스텔 연노랑 */
        border-left: 4px solid #FACC15; /* 진한 옐로우 포인트 */
        border-radius: 8px;
        padding: 10px 14px;
        margin-bottom: 10px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 14px;
        color: #333333; /* 진회색 텍스트 */
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }
    .memory-text {
        font-weight: 500;
        color: #333333;
    }

    /* 팁 박스 */
    .tip-box {
        background: #FFFBEB; border: 1px solid #FCD34D; border-radius: 12px;
        padding: 16px; font-size: 12px; color: #92400E; line-height: 1.5; margin-top: 20px;
    }

    /* 상품 카드 */
    .product-card {
        background: #ffffff !important;
        border: 1px solid #e5e7eb !important;
        border-radius: 14px !important;
        padding: 15px; text-align: center; height: 100%; 
        display: flex; flex-direction: column; justify-content: space-between;
        box-shadow: 0 4px 6px rgba(0,0,0,0.03);
        transition: transform 0.2s;
    }
    .product-card:hover { transform: translateY(-2px); box-shadow: 0 10px 15px rgba(0,0,0,0.08); }
    .product-img { width: 100%; height: 150px; object-fit: contain; margin-bottom: 12px; }
    .product-title { font-weight: 700; font-size: 14px; margin-bottom: 4px; }
    .product-price { color: #2563EB; font-weight: 700; margin-bottom: 10px; }
    
    /* 첫 페이지 안내 문구 */
    .warning-text {
        font-size: 13px; color: #DC2626; background: #FEF2F2; 
        padding: 10px; border-radius: 6px; margin-top: 4px; margin-bottom: 12px;
        border: 1px solid #FECACA;
    }

    .info-text {
        font-size: 14px; color: #374151; background: #F3F4F6;
        padding: 15px; border-radius: 8px; margin-bottom: 30px;
        border-left: 4px solid #DC2626;; line-height: 1.6;
    }

    /* ----------------------------- */
    /*  제목 크기 전체 축소 (h1~h3)  */
    /* ----------------------------- */

    h1, .stMarkdown h1 {
        font-size: 1.6rem !important;    /* 기존보다 약 -35% */
        font-weight: 700 !important;
    }

    h2, .stMarkdown h2 {
        font-size: 1.3rem !important;
        font-weight: 600 !important;
    }

    h3, .stMarkdown h3 {
        font-size: 1.15rem !important;
        font-weight: 600 !important;
    }

</style>
""", unsafe_allow_html=True)

# =========================================================
# 3. SYSTEM PROMPT (헤드셋 전용 + 메모리/프로필 강조)
# =========================================================
SYSTEM_PROMPT = r"""
너는 'AI 쇼핑 도우미'이며 **항상 블루투스 헤드셋** 기준을 파악해 추천을 돕는 역할을 한다.
스마트폰, 노트북, 태블릿, 일반 전자기기 등 다른 카테고리에 대한 추천이나 질문 유도는 절대 하지 않는다.
이어폰, 인이어 타입, 유선 헤드셋도 추천하지 않는다. 대화 전 과정에서 '블루투스 헤드셋'만을 전제로 생각한다.
단 질문을 던질 떄마다 특정 기준에 대해 어떻게 생각하시나요보다는 어떤 기준들을 고려할 수 있고, 그 기준들 중에서 무엇을 선호하는지 중심으로 물어본다.
사용자의 상황이나(야외, 실내 중 주로 어디서 사용하는지)나 아니면 상황적인 맥락(오랜 시간 착용하실 계획이신가요?)에 따라서 적절한 기준을 제안하거나 한다.(이런 부분을 제가 잘 고려할게요.)
어떤 것을 가장 선호하냐는 질문은 음질, 배터리, 노이즈캔슬링에서는 적합하지 않으며, 이 기준들은 제안으로 질문한다.

[역할 규칙]
- 최우선 규칙: 메모리에 이미 저장된 기준(특히 용도, 상황, 기능)은 절대 다시 물어보지 않고 바로 다음 단계의 구체적인 질문으로 전환한다.
- 너의 가장 큰 역할은 **사용자 메모리(쇼핑 기준 프로필)를 읽고, 갱신하고, 설명하면서 추천을 돕는 것**이다.
- 메모리에 이미 저장된 내용(특히 용도, 상황, 기능, 색상, 스타일 등)은 **다시 묻지 말고**, 그 다음 단계의 구체적인 질문으로 넘어간다.
- 메모리에 실제 저장될 경우(제어창에), 이 기준을 기억해둘게요" 혹은 "이번 쇼핑에서는 해당 내용을 고려하지 않을게요", “지금 말씀해주신 내용은 메모리에 추가해두면 좋을 것 같아요.”라고 표현을 먼저 제시한다.
- 사용자가 모호하게 말하면 부드럽게 구체적으로 다시 물어본다.
- 사용자가 기준을 바꾸거나 기존 메모리와 충돌하는 발화를 하면  
  “제가 기억하고 있던 내용은 ~였는데, 이번에는 기준을 바꾸실까요? 아니면 둘 다 함께 고려해볼까요?”라고 부드럽게 확인한다.
- 사용자가 “모르겠어요 / 글쎄요 / 아직 생각 안 했어요” 라고 말하면  
  “그렇다면 실제로 쓰실 상황을 떠올려보면 어떨까요? 출퇴근, 공부, 게임 중에 어떤 상황이 가장 많을까요?”처럼 맥락 중심으로 되묻거나, "제 생각은 이 기준이 중요하게 고려되면 좋을 것 같아요."로 안내한다.


[반복·성능 답정너 금지 규칙 — 매우 중요]
- 사용자가 '음악 감상'을 언급하더라도 절대 '음질 선호 여부'를 반복적으로 묻지 않는다.
- '고음/중음/저음'과 같은 음역대 취향을 묻는 follow-up 질문은 절대 금지한다.
- 이미 음질을 한 번 물어본 적이 있다면 다시 묻지 않는다.
- 착용감/노이즈캔슬링/배터리 같은 단일 성능 기준을 
  '혹시 이것도 중요하신가요?' 형태의 답정너 질문으로 유도하지 않는다.
- 사용자가 먼저 언급한 기준만 자연스럽게 확장해서 묻고, 
  사용자가 말하지 않은 기준은 제안하거나 자동으로 끌어오지 않는다.
- 사용자에게 ‘음질 선호(저음/중음/고음)’처럼 세부적인 음향 특성을 묻는 follow-up 질문은 절대 하지 않는다. 음질이라는 기준 자체는 받을 수 있지만, 세부 음역대 관련 질문은 금지한다.

[대화 흐름 규칙]
- 1단계(explore): 사용자가 사전에 입력한 정보 + 대화 중 발화를 바탕으로,  
  **용도/상황, 음질, 착용감, 노이즈캔슬링, 배터리, 디자인/스타일, 색상, 예산**에 대해서 사용자의 기존 메모리와 사용 목적의 용도에 기반해 자연스럽게 어떤 것을 중요시 여기는지 묻거나 제안한다.
- “가장 중요한 기준”이 있으면 그 기준을 먼저 다뤄야 한다.
  - 예: (가장 중요)가 디자인/스타일 → 기능 질문보다 **디자인/스타일 + 색상** 관련 질문을 먼저.
  - 예: (가장 중요)가 가격/가성비 → 다른 질문보다 **예산/가격대**를 먼저.
- “최우선 기준”이 없는 경우에만 기본 순서를 따른다:  
  용도/상황 → 노이즈캔슬링/음질 → 착용감/배터리 → 예산
- 이미 메모리에 있는 항목은 다시 물어보지 않고 다음 기준으로 넘어간다.
- 추천 단계로 넘어가기 전에 **예산**은 반드시 한 번은 확인해야 한다.
- 마지막으로 예산까지 다 채워져 요약 및 추천 단계로 넘어가기 전, 최우선 기준이 결국 무엇인지 무조건 물어본다.
- (중요) 메모리가 6개 이상이면 "지금까지 기준을 정리해드릴까요?"라고 추천하기 버튼을 제공하는 단계로 넘어간다.
- 메모리 기입할 때, 사용자의 발화를 그대로 기입하지 않고, 메모리 양식에 맞게 바꾼다.
- 사용자에게 ‘음질 선호(저음/중음/고음)’처럼 세부적인 음향 특성을 묻는 follow-up 질문은 절대 하지 않는다. 음질이라는 기준 자체는 받을 수 있지만, 세부 음역대 관련 질문은 금지한다.
- 사용자가 ~가 뭐야?, ~가 중요할까? 등 답변이 아닌 질문을 던질 경우, 기준 확인을 위한 질문 대신 답변을 우선적으로 진행하며, 기준으로 쌓아가도록 리드한다.

[메모리 활용 규칙]
- 대답할 때, 이전 메모리와 새롭게 추가된 메모리가   
  “제가 기억하고 있는 ○○님 취향은 ~였는데요, 그 기준에 비추어 보면 이 선택은 ~ 부분에서 잘 맞을 것 같아요.”  
 처럼 **메모리와 현재 추천을 연결해서 설명**한다.
- 
- 메모리와 최신 발화가 충돌하면  
  “예전에 말씀해주신 내용과 조금 다른데, 이번에는 새 기준을 우선해서 반영할까요?”라고 확인한다.
- 메모리에 색상/디자인/예산이 이미 있으면,  
  “기억하고 있는 메모리 기준(예: 블랙 선호, 가성비 중심)을 바탕으로 후보를 추려볼게요.”처럼 반드시 언급해 준다.
  
[메모리 기반 대화 연결 규칙 – 추가]
- 새로운 기준에 대해 질문할 때에도, 가능하면 항상 이전 메모리와 연결해서 말한다.
  - 예: “이전에 출퇴근용으로 쓰신다고 하셔서 여쭤보는데요, 그런 상황에서는 착용감과 노이즈캔슬링 중에 어떤 쪽을 조금 더 중요하게 보실까요?”
  - 예: “전에 디자인을 중요하게 보신다고 하셔서, 색상 쪽도 같이 생각해보시면 좋을 것 같아요. 혹시 선호하시는 색상이 있으실까요?”
- 그냥 “착용감도 중요하신가요?” 와 같이 뜬금없이 단일 기준을 던지지 말고,
  항상 “이전에 ~라고 말씀해 주셔서” / “방금 말씀하신 ~를 기준으로 보면” 같은 연결 구문을 한 번 넣어준다.
- 한 턴에 너무 많은 기준을 나열하지 말고, 기존 메모리 중 1개만 골라서 자연스럽게 이어서 물어본다.


[출력 규칙]
- 한 번에 질문은 1개만, 자연스러운 짧은 턴으로 나눈다.
- 중복 질문이 필요할 때에는 1번만 가능하며, 그것도 "정확한 추천을 위해 한 번만 다시 확인할게요."라고 이유를 덧붙인다.
- 항상 **헤드셋** 기준으로만 말하며, 다른 기기(스마트폰, 노트북 등)은 예로만 언급하더라도 추천 대상이 되지 않게 한다.
- 말투는 부드러운 존댓말을 유지하되, 너무 딱딱하지 않게 대화하듯 말한다.
"""

# =========================================================
# 4. 유틸리티 함수 (조사, 정규화 등)
# =========================================================
def get_eul_reul(noun: str) -> str:
    """을/를 자동 선택"""
    if not noun:
        return "을"
    last_char = noun[-1]
    if not ('\uAC00' <= last_char <= '\uD7A3'):
        return "를"
    last_char_code = ord(last_char) - 0xAC00
    jong = last_char_code % 28
    return "를" if jong == 0 else "을"


def naturalize_memory(text: str) -> str:
    """메모리 문장을 통일된 형태로 정리"""
    t = text.strip()
    t = t.replace("노이즈 캔슬링", "노이즈캔슬링")
    is_priority = "(가장 중요)" in t
    t = t.replace("(가장 중요)", "").strip()

    t = re.sub(r'로 생각하고 있어요\.?$', '', t)
    t = re.sub(r'이에요\.?$', '', t)
    t = re.sub(r'에요\.?$', '', t)
    t = re.sub(r'다\.?$', '', t)

    t = t.replace('비싼것까진 필요없', '비싼 것 필요 없음')
    t = t.replace('필요없', '필요 없음')

    t = re.sub(r'(을|를)\s*선호$', ' 선호', t)
    t = re.sub(r'(을|를)\s*고려하고$', ' 고려', t)
    t = re.sub(r'(이|가)\s*필요$', ' 필요', t)
    t = re.sub(r'(에서)\s*들을$', '', t)

    t = t.strip()
    if is_priority:
        t = "(가장 중요) " + t
    return t

def is_negative_response(text: str) -> bool:
    """
    사용자가 특정 질문에 대해 '없어 / 몰라 / 잘 모르겠어 / 별로 / 그만 / 관심없어' 등
    부정적이거나 회피하는 반응을 했는지 판별하는 함수.
    """
    if not text:
        return False

    negative_keywords = [
        # 기준이 없거나 애매함
        "없어", "없다고", "몰라", "모르겠", "잘 모르",
        "글쎄", "애매", "딱히",

        # 관심/중요도 낮음
        "별로", "아닌데", "굳이", "괜찮",
        "그만", "필요없", "필요 없", "상관없", "관심없", "안중요",

        # 우선순위를 못 정하는 답변 → 더 물어보지 말기
        "둘다 중요", "둘 다 중요", "둘 다 다 중요", "둘 다 괜찮",
        "둘다 괜찮", "다 중요해", "둘 다 비슷", "거의 비슷"
    ]

    return any(k in text for k in negative_keywords)

def extract_memory_with_gpt(user_input: str, memory_text: str):
    """
    GPT에게 사용자 발화에서 저장할 만한 '헤드셋 쇼핑 메모리'를 뽑게 하는 함수.
    JSON 형태로만 응답하게 해서 안정적으로 파싱.
    """
    prompt = f"""
당신은 '헤드셋 쇼핑 메모리 요약 AI'입니다.

사용자 발화:
\"\"\"{user_input}\"\"\"

현재까지 저장된 메모리:
{memory_text if memory_text else "(없음)"}

위 발화에서 '추가하면 좋은 쇼핑 메모리'가 있다면 아래 JSON 형식으로만 답하세요.

{{
  "memories": [
      "문장1",
      "문장2"
  ]
}}

반드시 지킬 것:
- 메모리는 모두 '블루투스 헤드셋 쇼핑 기준'이어야 합니다.
- user_input을 그대로 복붙하지 말고, 기준 문장 형태로 가공해서 쓰세요.
- 아래 규칙들을 참고해 문장을 만들어도 좋습니다.

[변환 규칙 예시]
- 브랜드 언급 → "선호하는 브랜드는 ~ 쪽이에요."
- 착용감/귀 아픔/편안 → "착용감이 편한 제품을 선호하고 있어요."
- 음악/노래/감상 → "주로 음악 감상 용도로 사용할 예정이에요."
- 출퇴근 → "출퇴근 시 사용할 용도예요."
- 예쁜/디자인 → "트렌디한 디자인/스타일을 중요하게 생각해요."
- 화려/레트로 → "원하는 디자인/스타일이 뚜렷한 편이에요."
- 깔끔/심플/무난 → "심플한 디자인을 선호해요."
- 색상 언급 → "색상은 ~ 계열을 선호해요."
- 노이즈 → "노이즈캔슬링 기능을 고려하고 있어요."
- 예산 N만원 → "예산은 약 N만 원 이내로 생각하고 있어요."

만약 저장할 만한 메모리가 전혀 없다면
{{
  "memories": []
}}
만 출력하세요.
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )

    try:
        data = json.loads(res.choices[0].message.content)
        return data.get("memories", [])
    except Exception:
        return []

# =========================================================
# 5. 메모리 추가/수정/삭제
# =========================================================
def _is_color_memory(text: str) -> bool:
    """색상 관련 메모리인지 판별"""
    t = text.replace("(가장 중요)", "")
    if "색상" in t and "선호" in t:
        return True
    color_keywords = ["화이트", "블랙", "네이비", "퍼플", "실버", "그레이", "핑크", "보라", "골드"]
    return any(k in t for k in color_keywords)


def _after_memory_change():
    """
    메모리가 변경된 뒤 공통으로 해야 할 처리:
    - just_updated_memory / memory_changed 플래그
    - summary 단계면 요약 재생성
    - comparison 단계면 추천 상품 다시 계산
    (알림 문구는 각 함수(add/delete/update)에서 개별 설정)
    """
    st.session_state.just_updated_memory = True
    st.session_state.memory_changed = True

    # summary 단계에서 메모리가 바뀌면 요약도 같이 다시 만들어주기
    if st.session_state.stage == "summary":
        st.session_state.summary_text = build_summary_from_memory(
            st.session_state.nickname,
            st.session_state.memory,
        )

    # comparison 단계에서 메모리가 바뀌면 추천 리스트도 다시 만들기
    if st.session_state.stage == "comparison":
        st.session_state.recommended_products = make_recommendation()

def add_memory(mem_text: str, announce: bool = True):
    mem_text = mem_text.strip()
    if not mem_text:
        return

    # 1) 정규화
    mem_text = naturalize_memory(mem_text)
    mem_text_stripped = mem_text.replace("(가장 중요)", "").strip()

    # 2) 예산 중복 제거
    if "예산은 약" in mem_text_stripped:
        st.session_state.memory = [
            m for m in st.session_state.memory if "예산은 약" not in m
        ]

    # 3) 색상 중복 제거
    if _is_color_memory(mem_text_stripped):
        st.session_state.memory = [
            m for m in st.session_state.memory if not _is_color_memory(m)
        ]

    # 4) 기존 메모리와 내용이 겹칠 때
    for i, m in enumerate(st.session_state.memory):
        base = m.replace("(가장 중요)", "").strip()

        if mem_text_stripped in base or base in mem_text_stripped:

            # ---------- (가장 중요) 승급 ----------
            if "(가장 중요)" in mem_text and "(가장 중요)" not in m:

                st.session_state.memory = [
                    mm.replace("(가장 중요)", "").strip()
                    for mm in st.session_state.memory
                ]

                st.session_state.memory[i] = mem_text

                if announce:
                    st.session_state.notification_message = "🌟 최우선 기준으로 설정되었어요."

                    # 🔥 로그 - 승급 기록
                    log_event(
                        "memory_priority_set",
                        new_value=mem_text,
                        memory_count=len(st.session_state.memory)
                    )

                _after_memory_change()
                return

            return  # 중복이면 끝

    # ---------- 5) 새로운 메모리 추가 ----------
    st.session_state.memory.append(mem_text)

    if announce:
        st.session_state.notification_message = "🧩 메모리에 새로운 내용을 추가했어요."

    # 🔥 로그 - 새 메모리 추가 기록
    log_event(
        "memory_add",
        new_value=mem_text,
        memory_count=len(st.session_state.memory)
    )

    _after_memory_change()

def delete_memory(index: int, source="agent"):
    """메모리 삭제"""
    if index < 0 or index >= len(st.session_state.memory):
        return
    
    old_value = st.session_state.memory[index]

    # 메모리 삭제
    st.session_state.memory.pop(index)

    # 🔥 로그 기록
    log_event(
        "memory_delete",
        old_value=old_value,
        memory_count=len(st.session_state.memory)
    )

    st.session_state.notification_message = "🗑️ 메모리에서 항목을 삭제했어요."
    _after_memory_change()

def update_memory(idx: int, new_text: str):
    """메모리 수정"""
    if not (0 <= idx < len(st.session_state.memory)):
        return

    new_text = naturalize_memory(new_text).strip()

    # 기존 값 저장 (old_value)
    old_value = st.session_state.memory[idx]

    # '(가장 중요)' 태그가 포함되면 다른 메모리에서는 모두 제거
    if "(가장 중요)" in new_text:
        st.session_state.memory = [
            m.replace("(가장 중요)", "").strip()
            for m in st.session_state.memory
        ]

    # 실제 메모리 변경
    st.session_state.memory[idx] = new_text

    # 🔥 로그 - 수정 기록 (항상 발생해야 함)
    log_event(
        "memory_update",
        old_value=old_value,
        new_value=new_text,
        index=idx,
        memory_count=len(st.session_state.memory)
    )

    st.session_state.notification_message = "🔄 메모리가 수정되었어요."
    _after_memory_change()

# =========================================================
# 6. 요약/추천 관련 유틸
# =========================================================
def extract_budget(mems):
    for m in mems:
        m1 = re.search(r"(\d+)\s*만\s*원", m)
        if m1:
            return int(m1.group(1)) * 10000
        txt = m.replace(",", "")
        m2 = re.search(r"(\d{2,7})\s*원", txt)
        if m2:
            return int(m2.group(1))
    return None


def detect_priority(mem_list):
    if not mem_list:
        return None
    for m in mem_list:
        if "(가장 중요)" not in m:
            continue
        m_low = m.lower()
        if any(k in m_low for k in ["디자인", "스타일", "깔끔", "미니멀", "레트로", "트렌디", "design", "style"]):
            return "디자인/스타일"
        if any(k in m_low for k in ["음질", "sound", "audio"]):
            return "음질"
        if any(k in m_low for k in ["착용감", "편안", "comfortable", "가벼운"]):
            return "착용감"
        if any(k in m_low for k in ["노이즈", "캔슬링"]):
            return "노이즈캔슬링"
        if any(k in m_low for k in ["배터리", "battery", "오래 쓰"]):
            return "배터리"
        if any(k in m_low for k in ["가격", "예산", "가성비", "price", "저렴", "싼", "싸게"]):
            return "가격/예산"
        if any(k in m_low for k in ["브랜드", "인지도", "유명"]):
            return "브랜드"
        return m.replace("(가장 중요)", "").strip()
    return None

import random

import random

def generate_personalized_reason(product, mems, name):
    reasons = []
    mem_str = " ".join(mems)
    tags = product.get("tags", [])

    # ============================================
    # 🔥 핵심 가치 기반 초간단 요약 (카드용)
    # ============================================
    # 우선순위: 메모리 → 제품 태그 순으로 하나 또는 두 개만 선택

    if "음질" in mem_str and "음질" in tags:
        reasons.append("음질 중심 사용자에게 잘 맞아요.")

    if "착용감" in mem_str and any(t in tags for t in ["편안함", "경량", "가벼움", "착용감"]):
        reasons.append("외부에서 쓰거나 장시간 착용 용도로 적합해요.")

    if "노이즈캔슬링" in mem_str and "노이즈캔슬링" in tags:
        reasons.append("노이즈캔슬링 성능이 뛰어나요.")

    # 제품 태그 기반 보조 문장
    if "배터리" in tags:
        reasons.append("배터리가 오래가는 편이에요.")

    if "가성비" in tags:
        reasons.append("가성비가 뛰어난 선택이에요.")

    if "통화품질" in tags:
        reasons.append("통화 품질도 준수해서 업무용으로 좋아요.")

    if "음질" in tags and "음질" not in mem_str:
        reasons.append("음질 평가도 좋아요.")

    # ============================================
    # ✨ 마지막 문장 — 제품 특성과 사용자 취향 기반 랜덤 선택
    # ============================================

    closing_templates = [
        f"{name}님의 상황과 잘 맞는 조합이에요!",
        f"{name}님이 선호하시는 기준과 잘 어울리는 제품이에요.",
        f"여러 기준을 고려하면 {name}님께 특히 잘 맞을 것 같아요.",
        f"{name}님의 사용 스타일과 궁합이 좋아 보여요!",             
        f"후기가 좋아서 {name}님에게도 좋은 평가를 받을 것 같아요:)",
        f"{name}님이 말씀하신 조건들과 자연스럽게 맞닿아 있어요."
    ]

    # 태그 기반 특정 버전 추가
    if "음질" in tags:
        closing_templates.append(f"특히 음질을 중시하는 {name}님께 잘 맞는 타입이에요.")
    if "배터리" in tags:
        closing_templates.append(f"오래 쓰는 사용 패턴을 가진 {name}님께도 잘 맞아요.")
    if "가성비" in tags:
        closing_templates.append(f"실속 있는 선택을 찾는 {name}님께 잘 어울려요.")

    reasons.append(random.choice(closing_templates))

    # ============================================
    # 중복 제거 + 2~3줄 이내로 제한
    # ============================================
    unique_reasons = []
    for r in reasons:
        if r not in unique_reasons:
            unique_reasons.append(r)

    # 카드에는 너무 길면 안되므로 2~3개 정도만 노출
    return "\n".join(unique_reasons[:3])

def send_product_detail_message(product):
    """
    선택된 제품의 상세 정보를 '채팅 메시지' 형태로 한 번에 보내는 함수.
    별도 페이지로 이동하지 않고, 대화 흐름 안에서 보여주기 위함.
    """
    detail_text = (
        f"📌 **{product['name']} 상세 정보 안내드릴게요!**\n\n"
        f"- **가격:** {product['price']:,}원\n"
        f"- **평점:** ⭐ {product['rating']:.1f} (리뷰 {product['reviews']}개)\n"
        f"- **주요 특징(태그):** {', '.join(product.get('tags', []))}\n"
        f"- **리뷰 한 줄 요약:** {product.get('review_one', '리뷰 요약 정보가 없습니다.')}\n\n"
        "🔄 현재 추천 상품이 마음에 들지 않으신가요?\n"
        "좌측 **쇼핑 메모리**를 수정하시면 추천 후보가 바로 달라질 수 있어요.\n"
        "예를 들어 예산, 색상, 노이즈캔슬링, 착용감 같은 기준을 바꿔보셔도 좋습니다.(특히 예산, 색상을 변경하면 바뀔 수 있어요.).\n\n"
        "이 제품에 대해 더 궁금한 점이 있으시면 편하게 물어봐 주세요 🙂 (예시 : 부정적 리뷰는 뭐가 있어?, 배터리 성능은 어떨까?) "
    )
    ai_say(detail_text)

# =========================================================
# 7. 상품 카탈로그 (기존 그대로)
# =========================================================
CATALOG = [
    {"name": "Anker Soundcore Q45", "brand": "Anker", "price": 179000, "rating": 4.4, "reviews": 1600, "rank": 8, "tags": ["가성비", "배터리", "노이즈캔슬링", "편안함"], "review_one": "가격 대비 성능이 훌륭하고 배터리가 길어요.", "color": ["블랙", "화이트", "네이비"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Anker%20Soundcore%20Q45.jpg"},
    {"name": "JBL Tune 770NC", "brand": "JBL", "price": 99000, "rating": 4.4, "reviews": 2300, "rank": 9, "tags": ["가벼움", "음질", "노이즈캔슬링", "편안함"], "review_one": "가볍고 음질이 좋다는 평이 많아요.", "color": ["블랙", "화이트", "퍼플", "네이비"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/JBL%20Tune%20770NC.png"},
    {"name": "Sony WH-CH720N", "brand": "Sony", "price": 129000, "rating": 4.5, "reviews": 2100, "rank": 6, "tags": ["노이즈캔슬링", "가벼움", "무난한 음질"], "review_one": "경량이라 출퇴근용으로 좋다는 후기가 많아요.", "color": ["블랙", "화이트", "블루"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Sony%20WH-CH720N.jpg"},
    {"name": "Bose QC45", "brand": "Bose", "price": 420000, "rating": 4.7, "reviews": 2800, "rank": 2, "tags": ["가벼움", "착용감", "노이즈캔슬링", "편안함"], "review_one": "장시간 써도 귀가 편하다는 리뷰가 많아요.", "color": ["블랙"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Bose%20QC45.jpg"},
    {"name": "Sony WH-1000XM5", "brand": "Sony", "price": 210000, "rating": 4.8, "reviews": 3200, "rank": 1, "tags": ["노이즈캔슬링", "음질", "착용감", "통화품질"], "review_one": "소음 많은 환경에서 확실히 조용해진다는 평가.", "color": ["핑크"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Sony%20WH-1000XM5.jpg"},
    {"name": "Apple AirPods Max", "brand": "Apple", "price": 679000, "rating": 4.6, "reviews": 1500, "rank": 3, "tags": ["브랜드", "노이즈캔슬링", "트렌디", "디자인", "고급"], "review_one": "깔끔한 디자인과 가벼운 무게로 만족도가 높아요.", "color": ["실버", "스페이스그레이"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Apple%20Airpods%20Max.jpeg"},
    {"name": "Sennheiser PXC 550-II", "brand": "Sennheiser", "price": 289000, "rating": 4.3, "reviews": 1200, "rank": 7, "tags": ["착용감", "여행", "배터리", "노이즈캔슬링"], "review_one": "여행 시 장시간 착용에도 압박감이 덜해요.", "color": ["블랙"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Sennheiser%20PXC%2055.jpeg"},
    {"name": "AKG Y600NC", "brand": "AKG", "price": 149000, "rating": 4.2, "reviews": 1800, "rank": 10, "tags": ["균형 음질", "가성비", "노이즈캔슬링"], "review_one": "가격대비 깔끔하고 균형 잡힌 사운드가 좋아요.", "color": ["블랙", "골드", "네이비"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/AKG%20Y6.jpg"},
    {"name": "Microsoft Surface Headphones 2", "brand": "Microsoft", "price": 319000, "rating": 4.5, "reviews": 900, "rank": 11, "tags": ["업무", "통화품질", "디자인", "노이즈캔슬링"], "review_one": "업무용으로 완벽하며 통화 품질이 매우 깨끗합니다.", "color": ["화이트", "블랙"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Microsoft%20Surface%20Headphones%202.jpeg"},
    {"name": "Bose Noise Cancelling Headphones 700", "brand": "Bose", "price": 490000, "rating": 4.7, "reviews": 2500, "rank": 4, "tags": ["노이즈캔슬링", "배터리", "음질", "프리미엄"], "review_one": "노이즈캔슬링 성능과 음질을 모두 갖춘 최고급 프리미엄 제품.", "color": ["블랙", "화이트"], "img": "https://raw.githubusercontent.com/doingsilvr/Shoppingagent/main/shoppingagent/img/Bose%20Headphones%20700.jpg"},
]

def _brief_feature_from_item(c):
    tags_str = " ".join(c.get("tags", []))
    if "가성비" in tags_str:
        return "가성비 인기"
    if c.get("rank", 999) <= 3:
        return "이달 판매 상위"
    if "디자인" in tags_str:
        return "디자인 강점"
    return "실속형 추천"


# =========================================================
# 8. GPT 응답 로직
# =========================================================
def get_product_detail_prompt(product, user_input):
    memory_text = "\n".join([naturalize_memory(m) for m in st.session_state.memory])
    nickname = st.session_state.nickname
    budget = extract_budget(st.session_state.memory)

    budget_line = ""
    budget_rule = ""

    if budget and st.session_state.product_detail_turn == 0:
        if product["price"] > budget:
            budget_line = f"- 사용자가 설정한 예산: 약 {budget:,}원"
            budget_rule = (
                f"4. (첫 답변에서만 적용)\n"
                f"   가격이 예산을 초과한 경우, 답변 첫 문장에 다음 문구 포함:\n"
                f"   - “예산(약 {budget:,}원)을 약간 초과하지만…”\n"
            )

    return f"""
당신은 지금 '상품 상세 정보 단계(product_detail)'에 있습니다.
이 단계에서는 사용자가 선택한 **블루투스 헤드셋 한 제품만** 명확하고 사실 기반으로 설명합니다.

[사용자 질문]
"{user_input}"

[선택된 제품 정보]
- 제품명: {product['name']} ({product['brand']})
- 가격: {product['price']:,}원
- 색상 옵션: {', '.join(product['color'])}
- 평점: {product['rating']:.1f}
- 주요 특징: {', '.join(product['tags'])}
- 리뷰 요약: {product['review_one']}
{budget_line}

[응답 규칙]
1. 질문에 대한 핵심 정보만 간단히 답변합니다.
2. 다른 제품과의 비교나 추천 리스트 언급은 하지 않습니다.
3. "현재 선택된 이 헤드셋은~"처럼, 항상 헤드셋 기준으로 설명합니다.
4. 탐색 질문(용도/기준 재질문)은 하지 않습니다.
{budget_rule}5. 답변 마지막 문장은 다음 중 하나로 끝냅니다:
   - "다른 부분도 더 궁금하신가요?"
   - "추가로 알고 싶은 점 있으신가요?"
   - "결정을 내리셨다면 언제든지 구매결정하기 버튼을 누르실 수 있습니다!"

위 규칙을 지키며 자연스럽고 간결한 한국어로 답변하세요.
"""

def gpt_reply(user_input: str) -> str:
    """GPT가 단계(stage)별로 다르게 응답하도록 제어하는 핵심 함수"""

    ss = st.session_state
    memory_text = "\n".join([naturalize_memory(m) for m in ss.memory])
    nickname = ss.nickname
    stage = ss.stage

    # context_setting_page에서 세팅한 최우선 기준
    primary_style = ss.get("primary_style", "")   # "price" / "design" / "performance"
    has_budget = any("예산" in m for m in ss.memory)

    # =========================================================
    # 1) product_detail 단계: 전용 프롬프트 강제 사용
    # =========================================================
    if stage == "product_detail":
        product = ss.selected_product
        if not product:
            ss.stage = "comparison"
            return "선택된 제품 정보가 없어서 추천 목록으로 다시 돌아갈게요!"

        prompt = get_product_detail_prompt(product, user_input)

        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.35,
        )
        ss.product_detail_turn += 1
        return res.choices[0].message.content

    # =========================================================
    # 2) 탐색(explore) / 요약(summary) / 비교(comparison) 단계
    # =========================================================
    stage_hint = ""

    # 🔒 항상 헤드셋 대화 규칙
    stage_hint += (
        "[중요 규칙] 이 대화는 항상 '블루투스 헤드셋' 기준입니다. "
        "스마트폰·노트북 등 다른 기기 추천이나 질문은 하지 마세요.\n\n"
    )

    # ---------------------------------------------------------
    # A. 디자인/스타일 관련 정보
    # ---------------------------------------------------------
    design_keywords = ["디자인", "스타일", "예쁜", "깔끔", "세련", "미니멀", "레트로", "감성", "스타일리시"]

    is_design_in_memory = any(
        any(k in m for k in design_keywords)
        for m in ss.memory
    )

    design_priority = (
        primary_style == "design" or
        any("(가장 중요)" in m and any(k in m for k in design_keywords) for m in ss.memory)
    )

    # 색상 정보 있는지
    has_color_detail = any("색상" in m for m in ss.memory)

    # ---------------------------------------------------------
    # B. explore 단계에서 ‘디자인이 최우선’이면
    #    → 이번 턴엔 반드시 ‘디자인 or 색상’ 질문만 1개
    # ---------------------------------------------------------
    if stage == "explore" and design_priority:
        stage_hint += """
[디자인/스타일 최우선 규칙 – 이번 턴 필수]
- 이번 턴에는 반드시 ‘디자인’ 또는 ‘색상’ 관련 질문 **단 1개**만 하세요.
- 음질/착용감/배터리/노이즈캔슬링 등 기능 질문은 **이번 턴에서 금지**합니다.
- 이미 색상 정보를 알고 있다면 디자인 스타일(깔끔/레트로/포인트 컬러 등)만 물어보세요.
"""

    # ---------------------------------------------------------
    # C. 가격/가성비 최우선이면 → 예산 먼저
    # ---------------------------------------------------------
    if stage == "explore" and primary_style == "price" and not has_budget:
        stage_hint += """
[가격/가성비 최우선 규칙 – 이번 턴 필수]
- 이번 턴에는 반드시 예산/가격대에 대해 한 가지만 물어보세요.
- 음질/노이즈캔슬링/착용감 등 기능 질문은 이번 턴에는 하지 마세요.
"""

    # ---------------------------------------------------------
    # D. explore 단계 — 용도는 이미 메모리에 있으면 절대 다시 묻지 않기
    # ---------------------------------------------------------
    usage_keywords = ["용도", "출퇴근", "운동", "게임", "여행", "공부", "음악 감상"]
    is_usage_in_memory = any(any(k in m for k in usage_keywords) for m in ss.memory)

    if stage == "explore" and is_usage_in_memory and len(ss.memory) >= 2:
        stage_hint += (
            "[용도 파악됨] 이미 사용 용도는 기억하고 있습니다. "
            "다시 묻지 말고 다음 기준(디자인/예산/음질/착용감 등)으로 넘어가세요.\n"
        )

    # ---------------------------------------------------------
    # E. GPT 본문 프롬프트 구성
    # ---------------------------------------------------------
    prompt_content = f"""
{stage_hint}

[현재 저장된 쇼핑 메모리]
{memory_text if memory_text else "(아직 없음)"}

[사용자 발화]
{user_input}

위 정보를 참고해서, '블루투스 헤드셋 쇼핑 도우미' 역할로서
다음 말을 자연스럽고 짧게 이어가세요.
"""

    # 실제 GPT 호출
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_content},
        ],
        temperature=0.45,
    )

    reply = res.choices[0].message.content

    # =========================================================
    # 🔥 F. 사후 필터링: '음질 먼저 묻기' 강제 차단
    # =========================================================
    if stage == "explore":
        # 1) 가성비 우선인데 예산 아직 없고, 답변이 음질 위주 → 예산 질문으로 강제 교체
        if primary_style == "price" and not has_budget:
            if any(k in reply for k in ["음질", "소리", "사운드"]) and not any(
                k in reply for k in ["예산", "가격", "얼마", "가격대"]
            ):
                reply = (
                    "가성비를 가장 중요하게 보신다고 하셔서, 먼저 예산 범위를 여쭤보고 싶어요.\n"
                    "대략 어느 정도 가격대를 생각하고 계신가요? (예: 10만 원대, 20만 원 이하 등)"
                )

        # 2) 디자인/스타일 최우선인데 음질 질문이 먼저 나오면 → 디자인/색상 질문으로 교체
        if design_priority:
            if any(k in reply for k in ["음질", "소리", "사운드"]) and not any(
                k in reply for k in design_keywords + ["색상"]
            ):
                reply = (
                    "디자인과 스타일을 가장 중요하게 보신다고 하셔서, 먼저 외형 쪽을 조금 더 여쭤보고 싶어요.\n"
                    "선호하시는 색상이나 분위기(깔끔한 느낌, 포인트 컬러, 레트로 느낌 등)가 있으신가요?"
                )

    return reply

# =========================================================
# 9. 로그 유틸
# =========================================================
def ai_say(text: str):

    # 🔥 AI 메시지 로그 기록 (이 줄 추가)
    log_event("assistant_message", text=text)
    
    st.session_state.messages.append({"role": "assistant", "content": text})

def user_say(text: str):
    st.session_state.messages.append({"role": "user", "content": text})
    st.session_state.turn_count += 1

# =========================================================
# 11. 단계 진행바 UI
# =========================================================
def render_step_header():
    stage = st.session_state.stage

    def is_active(step_name):
        return "step-active" if step_name == stage else ""

    step_items = f"""
    <div class="progress-container">
        <div class="step-item {is_active('explore')}">
            <div class="step-header-group">
                <div class="step-circle">1</div>
                <div class="step-title">기준 탐색</div>
            </div>
            <div class="step-desc">사용자의 취향과 기준을 파악하는 단계입니다.</div>
        </div>
        <div class="step-item {is_active('summary')}">
            <div class="step-header-group">
                <div class="step-circle">2</div>
                <div class="step-title">요약 확인</div>
            </div>
            <div class="step-desc">정리된 기준을 확인한 뒤 추천으로 이동합니다.</div>
        </div>
        <div class="step-item {is_active('comparison')}">
            <div class="step-header-group">
                <div class="step-circle">3</div>
                <div class="step-title">상품 추천</div>
            </div>
            <div class="step-desc">기준에 맞는 헤드셋을 비교합니다.</div>
        </div>
        <div class="step-item {is_active('product_detail')}">
            <div class="step-header-group">
                <div class="step-circle">4</div>
                <div class="step-title">상세 정보</div>
            </div>
            <div class="step-desc">선택한 제품의 상세 정보를 안내합니다.</div>
        </div>
        <div class="step-item {is_active('purchase_decision')}">
            <div class="step-header-group">
                <div class="step-circle">5</div>
                <div class="step-title">구매 결정</div>
            </div>
            <div class="step-desc">최종 결정을 진행하는 단계입니다.</div>
        </div>
    </div>
    """
    st.markdown(step_items, unsafe_allow_html=True)


# =========================================================
# 12. 좌측 메모리 패널
# =========================================================
def render_memory_sidebar():
    st.markdown("### 🧠 현재 나의 쇼핑 메모리")       
    
    # --------------------------
    # [1] 삭제 콜백 (에러 방지 핵심)
    # --------------------------
    def on_delete_click(index):
        # 삭제 후에는 자동으로 delete_memory 안에서 로그도 남기고
        # notification_message도 설정됩니다.
        delete_memory(index, source="user")

    mem_container = st.container()
    with mem_container:
        for i, mem in enumerate(st.session_state.memory):
            c1, c2 = st.columns([8, 2])
            with c1:
                st.markdown(
                    f"<div class='memory-block'><div class='memory-text'>{mem}</div></div>",
                    unsafe_allow_html=True
                )
            with c2:
                # key에 hash값 추가로 충돌 방지
                st.button(
                    "X", 
                    key=f"delete_btn_{i}_{hash(mem)}", 
                    on_click=on_delete_click, 
                    args=(i,)
                )

    st.markdown("<hr>", unsafe_allow_html=True)

    # --------------------------
    # [2] 추가 폼 (엔터키 입력 지원)
    # --------------------------
    st.markdown("**✏️ 직접 기준 추가하기**")

    with st.form(key="add_mem_form", clear_on_submit=True):
        new_mem = st.text_input(
            "추가할 기준",
            placeholder="예: 오래 써도 귀가 편하면 좋겠어요",
            label_visibility="collapsed"
        )
        submit = st.form_submit_button("메모리 추가하기")
        
        if submit and new_mem.strip():
            # 1) 로그 기록
            log_event(
                "memory_add",
                source="user",
                new_value=new_mem.strip(),
                memory_count=len(st.session_state.memory)
            )
            # 2) 메모리 추가
            # 이 함수 안에서 notification_message를 세팅해주므로 
            # 별도로 st.success를 쓸 필요가 없습니다.
            add_memory(new_mem.strip()) 
            
            # 3) 새로고침 (입력창 비우고 목록 갱신 + Toast 알림 표시)
            st.rerun()

# =========================================================
# 13. 추천 UI (3개 카드)
# =========================================================
# ============================================================
# 상품 상세 메시지 생성
# ============================================================
def format_product_detail_msg(product):
    features = ""
    if "features" in product:
        for f in product["features"]:
            features += f"- {f}\n"

    return f"""
[@{product['name']} 상세 정보]

📌 **가격:** {product['price']:,}원  
⭐ **평점:** {product['rating']:.1f}점 ({product['reviews']}개 리뷰)

**주요 특징:**  
{features if features else "등록된 상세 특징이 없어요."}

궁금하신 점을 자유롭게 물어보세요!  
예: "노이즈캔슬링 강한가요?", "착용감 어떤 편인가요?"
"""


# ============================================================
# 카드 하이라이트 CSS
# ============================================================
def inject_card_css():
    st.markdown("""
    <style>
    .product-card {
        transition: 0.15s ease;
        padding: 14px;
        border-radius: 14px;
        background: white;
        border: 1px solid #EEE;
    }
    .product-card.selected {
        border: 3px solid #4A8DFD !important;
        box-shadow: 0 0 15px rgba(74,141,253,0.4) !important;
        transform: scale(1.02);
    }
    </style>
    """, unsafe_allow_html=True)


# ============================================================
# 추천 UI (★ 완전 교체)
# ============================================================
import html

def recommend_products_ui(name, mems):
    products = st.session_state.recommended_products

    if not products:
        st.warning("추천을 위해 기준이 조금 더 필요해요!")
        return
        
    st.markdown("### 🔍 고객님을 위한 후보들을 비교해보세요!")
    st.markdown(
        "<p style='margin-top:-10px; color:#4B5563;'>"
        "1) ‘자세히 질문하기’를 눌러 각 후보에 대해 자유롭게 물어보실 수 있어요.(예 : 부정적인 리뷰는 어때? )<br>"
        "2) 어느 정도 후보가 추려지면 아래의 ‘구매하러 가기’를 눌러주세요!(1번을 수행해야 진행 가능)"
        "</p>",
        unsafe_allow_html=True,
    )

    # CSS
    st.markdown("""
        <style>
        .product-card {
            min-height: 360px;
            border-radius: 12px;
            padding: 15px;
            background: white;
            text-align: center;
            position: relative;
        }
        .product-img {
            width: 100%;
            border-radius: 10px;
            margin-bottom: 10px;
        }
        </style>
    """, unsafe_allow_html=True)

    cols = st.columns(3)

    for idx, p in enumerate(products):
        with cols[idx]:

            is_sel = (
                st.session_state.selected_product is not None and
                st.session_state.selected_product["name"] == p["name"]
            )

            border = "#2563EB" if is_sel else "#e5e7eb"
            badge = (
                '<div style="position:absolute; top:8px; right:8px; '
                'background:#2563EB; color:white; padding:3px 6px; '
                'border-radius:6px; font-size:11px;">선택됨</div>'
                if is_sel else ""
            )

            # ------- 여기! 한 줄씩 더하기 방식으로 변경 -------
            html_parts = []

            html_parts.append(f'<div class="product-card" style="border:2px solid {border};">')

            if badge:
                html_parts.append(badge)

            html_parts.append(f'<img src="{p["img"]}" class="product-img">')

            html_parts.append(f'<div style="font-weight:700; font-size:15px;">{p["name"]}</div>')
            html_parts.append(f'<div style="color:#2563EB; font-weight:600;">{p["price"]:,}원</div>')
            html_parts.append(f'<div style="font-size:13px; color:#6b7280;">⭐ {p["rating"]:.1f} / 리뷰 {p["reviews"]}</div>')

            html_parts.append(
                '<div style="margin-top:10px; font-size:13px; color:#4b5563;">'
                + html.escape(generate_personalized_reason(p, mems, name))
                + '</div>'
            )

            html_parts.append('</div>')

            # 👉 문자열을 join 해서 한 줄 HTML로 만듦 → 절대 깨지지 않음
            card_html = "".join(html_parts)

            st.markdown(card_html, unsafe_allow_html=True)
            
            if st.button("자세히 질문하기", key=f"detail_{p['name']}"):
                log_event(
                    "product_detail_enter",
                    value=p["name"],
                    index=idx,
                    memory_count=len(st.session_state.memory)  # ⭐ 중요
                )
                
                st.session_state.selected_product = p
                st.session_state.stage = "product_detail"
                st.session_state.product_detail_turn = 0
                
                send_product_detail_message(p)
                st.rerun()

    # -------------------------
    # 선택된 제품이 있을 때만 하단 결정 버튼
    # -------------------------
    if st.session_state.selected_product:
        p = st.session_state.selected_product

        st.markdown(
            f"""
            <div style="margin-top:15px; padding:12px 16px; background:#ECF5FF;
            border-radius:12px; font-size:15px; border:1px solid #cfe1ff;">
                ✔ <b>{p['name']}</b> 제품을 선택하셨어요.
                궁금한 점을 채팅을 통해 질문하실 수 있으며, 마음을 정하셨다면 아래 버튼으로 최종 결정을 진행할 수 있어요.
            </div>
            """,
            unsafe_allow_html=True
        )

        if st.button("🛒 구매하러 가기(Link)", key="final_decide_btn"):
            st.session_state.final_choice = p
            st.session_state.stage = "purchase_decision"

            # 🔥 최종 결정 로그
            log_event("final_decision", value=p["name"])
        
            # summary가 아직 안 작성되었을 때만 실행 🔥
            if not st.session_state.summary_written:
                success = write_session_summary()   # ← 성공(True) / 실패(False) 반환하도록 수정
                st.session_state.summary_written = success
                                 
            ai_say(f"좋습니다! **'{p['name']}'**(으)로 결정하셨군요! 이제 모든 실험이 끝났습니다. 설문페이지로 돌아가주세요:).")
            st.rerun()

    else:
        st.info("한 제품을 자세히 보고 싶으시면 위 카드 중 하나를 선택해 질문해주세요. 😊")

# =========================================================
# 14. 요약 생성 함수
# =========================================================
def build_summary_from_memory(name, mems):
    if not mems:
        return (
            f"{name}님, 아직 쇼핑 기준이 충분히 모이지 않았어요.\n"
            "조금만 더 알려주시면 더 정확한 추천을 도와드릴게요!"
        )

    # 메모리 정리
    cleaned = [m.replace("(가장 중요)", "").strip() for m in mems]
    lines = [f"- {c}" for c in cleaned]

    # 최우선 기준 찾기
    priority = None
    for m in mems:
        if "(가장 중요)" in m:
            priority = m.replace("(가장 중요)", "").strip()
            break

    # 🩶 기본 구조
    summary = f"""
[@{name}님의 쇼핑 기준 요약]

지금까지의 대화를 바탕으로 정리된 기준은 아래와 같습니다:

{chr(10).join(lines)}

"""

    # ⭐ 최우선 기준 강조
    if priority:
        summary += (
            f"이 중에서 특히 **'{priority}'** 기준을 가장 중요하게 보고 계신 것으로 이해했어요.\n\n"
        )

    # 💬 자연스러운 관찰형 문장 (예: 디자인 중심, 예산 범위 등)
    # → GPT가 만들어낸 맥락 기반 summary 느낌을 원한다면 여기에 한 줄 넣어도 됨
    # 하지만 UI 안정성을 위해 여기선 고정 문장만 둠

    summary += (
        "현재 말씀해주신 기준만으로도 충분히 추천을 드릴 수 있는 상태예요! 😊\n"
        "왼쪽의 ‘쇼핑 메모리’에서 기준을 직접 수정하거나 삭제하실 수도 있고,\n"
        "저에게 편하게 말씀해주셔도 바로 반영해드릴게요.\n\n"
        "준비되셨다면 아래의 **‘이 기준으로 추천 받기’** 버튼을 눌러주세요."
    )

    return summary.strip()

# =========================================================
# 15. 추천 모델 (메모리 기반 점수)
# =========================================================
def score_item_with_memory(item, mems):
    score = 0
    
    mtext = " ".join(mems)
    budget = extract_budget(mems)

    # (1) 최우선 기준 강점 보정
    if "(가장 중요)" in mtext:
        if "디자인/스타일" in mtext and "디자인" in item["tags"]:
            score += 50
        if "음질" in mtext and "음질" in item["tags"]:
            score += 50
        if "착용감" in mtext and "착용감" in item["tags"]:
            score += 50

    # (2) 일반 기준 반영
    for m in mems:
        if "노이즈" in m and "노이즈캔슬링" in item["tags"]:
            score += 20
        if "가성비" in m and "가성비" in item["tags"]:
            score += 20
        if "색상" in m:
            for col in item["color"]:
                if col in m:
                    score += 10

    # (3) 랭크 보정
    score -= item["rank"]

    # ---------------------------
    # (4) 🟡 예산 보정 — 가장 중요!
    # ---------------------------
    if budget:
        if item["price"] > budget:
            diff = item["price"] - budget
            if diff > 100000:          # 10만원 초과
                score -= 200
            else:
                score -= 80
        else:
            score += 30  # 예산 이내면 가산점

    return score

def make_recommendation():
    scored = [(score_item_with_memory(item, st.session_state.memory), item) for item in CATALOG]
    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[:3]]

# =========================================================
# 🔥 질문 ID → 실제 메모리 문장 변환 테이블 (전역)
# =========================================================
MAPPING = {
    "comfort": "착용감이 편한 제품을 선호하고 있어요.",
    "sound": "음질을 중요하게 생각하고 있어요.",
    "design": "디자인/스타일을 중요하게 보고 있어요.",
    "color": "선호하는 색상이 있어요.",
    "battery": "배터리 지속시간을 중요하게 생각하고 있어요.",
    "budget": "예산은 약 00만 원 이내로 생각하고 있어요."
}

# =========================================================
# 16. 사용자 입력 처리
# =========================================================
def handle_input():
    u = st.session_state.user_input_text.strip()
    if not u:
        return

    ss = st.session_state

    # ------------------------------
    # 1) 사용자 메시지 먼저 UI에 반영
    # ------------------------------
    user_say(u)
    log_event("user_message", text=u)

    # ------------------------------
    # 2) 현재 진행 중 질문 처리
    # ------------------------------
    cur_q = ss.current_question

    if cur_q:
        # 부정형 답변
        if is_negative_response(u):
            ss.question_history.append(cur_q)
            ss.current_question = None
            ai_say("네! 그럼 다음 기준으로 넘어가볼게요. 추가로 고려할 기준 있으신가요? (예: 색상·디자인·착용감·예산 등)")
            return

        # 긍정형 답변
        if any(u.startswith(k) or u == k for k in YES_KEYWORDS):
            if cur_q in MAPPING:
                add_memory(MAPPING[cur_q])
            ss.question_history.append(cur_q)
            ss.current_question = None
            ai_say("네! 반영해둘게요 😊 다른 기준도 있으신가요?")
            return

        # 일반 응답 → 질문 종료
        ss.question_history.append(cur_q)
        ss.current_question = None

    # ------------------------------
    # 3) 카테고리 드리프트 방지
    # ------------------------------
    drift_words = ["스마트폰", "휴대폰", "핸드폰", "아이폰", "갤럭시"]
    if any(w in u for w in drift_words):
        ai_say("앗! 지금은 헤드셋 추천 단계예요 😊 헤드셋 기준으로 도와드릴게요!")
        return

    # ------------------------------
    # 4) 메모리 추출
    # ------------------------------
    memory_before = ss.memory.copy()
    memory_text = "\n".join([naturalize_memory(m) for m in ss.memory])
    extracted = extract_memory_with_gpt(u, memory_text)

    if extracted:
        for mem in extracted:
            if mem not in ss.memory:
                add_memory(mem)
                ss.notification_message = f"🧩 '{mem}' 내용을 기억해둘게요."

    # ------------------------------
    # 5) SUMMARY 진입 조건
    # ------------------------------
    user_request_reco = any(k in u for k in ["추천", "골라줘", "추천해줘", "추천 받을게"])
    mem_count = len(ss.memory)
    has_budget = any("예산" in m for m in ss.memory)
    enough_memory = mem_count >= 5

    # ① 리뷰 요청 (사용자가 직접 추천 요청)
    if user_request_reco:
        if has_budget:
            ss.summary_text = build_summary_from_memory(ss.nickname, ss.memory)
            ss.stage = "summary"
            ai_say("좋아요! 지금까지의 기준을 정리해드릴게요 😊")
            return
        else:
            ss.current_question = "budget"
            ai_say("추천을 위해 예산을 알려주세요!")
            ss.summary_text = build_summary_from_memory(ss.nickname, ss.memory)
            return

    # ② 메모리 충분(자동 요약)
    if ss.stage == "explore" and enough_memory:
        if has_budget:
            ss.summary_text = build_summary_from_memory(ss.nickname, ss.memory)
            ss.stage = "summary"
            return
        else:
            ss.current_question = "budget"
            ai_say("기준이 충분히 모였어요! 예산은 어떻게 보고 계세요?")
            return

    # ------------------------------
    # 5) GPT 일반 응답 생성
    # ------------------------------
    reply = gpt_reply(u)
    ai_say(reply)

    # =======================================================
    # 🔥 6) GPT 질문 ID 감지 + 중복 질문 차단
    # =======================================================
    qid = None

    # 1) 질문 유형 감지
    if "디자인" in reply or "스타일" in reply:
        qid = "design"

    elif "색상" in reply and "선호" in reply:
        qid = "color"

    elif any(x in reply for x in ["음질", "소리", "사운드", "고음", "중음", "저음"]):
        qid = "sound"

    elif "착용감" in reply:
        qid = "comfort"

    elif "배터리" in reply:
        qid = "battery"

    elif "예산" in reply or "가격대" in reply:
        qid = "budget"

    # 2) 🔥 음질 질문 중복 차단 (변주 포함)
    if qid == "sound":
        if "sound" in ss.question_history:
            ss.current_question = None
            return

    # 3) 🔥 이미 했던 질문이면 무효화
    if qid and qid in ss.question_history:
        ss.current_question = None
        return

    # 4) 새 질문 저장
    ss.current_question = qid

    # =======================================================
    # 🔥 7) summary 단계에서의 처리
    # =======================================================
    if ss.stage == "summary":
        if any(k in u for k in ["좋아요", "네", "맞아요", "추천"]):
            ss.stage = "comparison"
            ss.recommended_products = make_recommendation()
            ai_say("좋아요! 지금까지의 기준을 기반으로 추천을 드릴게요.")
        else:
            ai_say(
                "수정하고 싶은 기준이 있으면 좌측 '쇼핑 메모리'에서 편하게 변경해주세요 😊"
            )
        return

# =========================================================
# 17. context_setting 페이지 (Q1/Q2 새 구조 적용)
# =========================================================
def context_setting_page():
    st.title("🛒 쇼핑 에이전트에게 정보를 알려주세요.")

    st.markdown(
        """
        <div class="info-text">
            본격적인 쇼핑 전, <b>AI 에이전트(쇼파)가 귀하의 쇼핑 경험, 취향 등</b>을 기억할 수 있도록 초기 메모리를 쌓기 위한 단계입니다.<br>
            평소 본인의 실제 쇼핑 기준이나 성향 등을 바탕으로 선택하면, 에이전트는 그 메모리에 저장한 후 이를 참고하며 대화를 이어가게 됩니다.
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.subheader("📝 기본 정보")

        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("이름", placeholder="홍길동")
            st.markdown(
                '<div class="warning-text">⚠️ 사전 설문과 동일한 이름으로 입력해주세요.</div>',
                unsafe_allow_html=True,
            )
        with col2:
            phone = st.text_input("전화번호 (뒷 4자리)", placeholder="1234")

        st.markdown("---")

        # -----------------------
        # Q1. 쇼핑 성향
        # -----------------------
        st.subheader("Q1. 아래 3가지 중, 본인과 가장 가까운 쇼핑 성향은 무엇인가요?")
        shopping_style = st.selectbox(
            "",
            ["가성비 우선형", "디자인/스타일 우선형", "성능·스펙 우선형"],
        )

        # -----------------------
        # Q2. 선호 색상
        # -----------------------
        st.subheader("Q2. 아래 색상 중, 제품을 고를 때 가장 먼저 눈이 가는 색상은 무엇인가요?")
        color_choice = st.selectbox(
            "",
            ["블랙", "화이트", "핑크", "네이비", "블루", "퍼플", "그레이"],
        )

        st.markdown("<br>", unsafe_allow_html=True)

        # -----------------------
        # 저장 버튼
        # -----------------------
        if st.button("쇼핑 시작하기(여러번 클릭)", type="primary", use_container_width=True):
            if not name:
                st.warning("이름을 입력해주세요.")
                return

            # 사용자 정보 저장
            st.session_state.nickname = name
            st.session_state.phone_number = phone

            # 🔹 우선 기준 기본값 초기화
            st.session_state.primary_style = ""
            st.session_state.priority_followup_done = False

            # 초기 메모리 + 우선 기준 유형 세팅
            if shopping_style == "가성비 우선형":
                add_memory("가성비, 가격을 중요하게 생각하는 편이에요.", announce=False)
                st.session_state.primary_style = "price"
                # 가격 기준은 예산이 곧 핵심이니까, 바로 예산 질문으로 넘어가도 괜찮으니 True
                st.session_state.priority_followup_done = True

            elif shopping_style == "디자인/스타일 우선형":
                add_memory("(가장 중요) 디자인/스타일을 최우선으로 고려하고 있어요.", announce=False)
                st.session_state.primary_style = "design"
                # 디자인 구체 질문은 아직 안 했으니 False 유지

            else:  # "성능·스펙 우선형"
                add_memory("(가장 중요) 성능/스펙을 우선하는 쇼핑 성향이에요.", announce=False)
                st.session_state.primary_style = "performance"
                # 성능 관련 구체 질문도 아직 안 했으니 False 유지

            add_memory(f"색상은 {color_choice} 계열을 선호해요.", announce=False)

            st.session_state.page = "chat"
            st.rerun()
            
# =========================================================
# 18. main_chat_interface (UI 그대로 사용)
# =========================================================
def main_chat_interface():

    # 🔒 안전 가드 — 세션이 완전 초기화되기 전에 호출될 때 에러 방지
    if "notification_message" not in st.session_state:
        st.session_state.notification_message = ""

    # 알림/토스트 처리
    if st.session_state.notification_message:
        try:
            st.toast(st.session_state.notification_message, icon="✅")
        except Exception:
            st.info(st.session_state.notification_message)
        st.session_state.notification_message = ""

    # 첫 메시지
    if len(st.session_state.messages) == 0:
        ai_say(
            f"안녕하세요 {st.session_state.nickname}님! 😊 저는 당신의 AI 쇼핑 도우미예요.\n"
            f"블루투스 헤드셋을 추천해달라고 하셨으니, 이와 관련해 {st.session_state.nickname}님에 대해 더 파악해볼게요. 주로 어떤 용도로 헤드셋을 사용하실 예정인가요?"
        )

    # 상단 UI
    render_step_header()

    col1, col2 = st.columns([3, 7], gap="large")

    with col1:
        render_memory_sidebar()

    with col2:
    
        # ---------------------------
        # 📌 채팅창 렌더링
        # ---------------------------
        chat_container = st.container()
        with chat_container:
    
            chat_html = "<div class='chat-display-area'>"
    
            for msg in st.session_state.messages:
                safe = html.escape(msg["content"]).replace("\n", "<br>")
                role = msg["role"]
    
                if role == "assistant":
                    chat_html += f"<div class='chat-bubble chat-bubble-ai'>{safe}</div>"
                else:
                    chat_html += f"<div class='chat-bubble chat-bubble-user'>{safe}</div>"
    
            # summary면 요약도 말풍선으로 추가
            if st.session_state.stage == "summary":
                summary_html = html.escape(st.session_state.summary_text).replace("\n", "<br>")
                chat_html += f"<div class='chat-bubble chat-bubble-ai'>{summary_html}</div>"
    
            chat_html += "</div>"
    
            st.markdown(chat_html, unsafe_allow_html=True)
    
        # ------------------------------
        # 🔥 추천 받기 버튼 — summary에서만!
        # ------------------------------
        if st.session_state.stage == "summary":
            if st.button("🔍 이 기준으로 추천 받기"):
                st.session_state.stage = "comparison"
                log_event("stage_change", new_value="comparison")
                st.session_state.recommended_products = make_recommendation()
    
                prods = st.session_state.recommended_products
                candidate_names = ",".join([p["name"] for p in prods]) if prods else ""
    
                log_event("show_candidates", value=candidate_names)
    
                name = st.session_state.nickname
                mems = st.session_state.memory
    
                ai_say(
                    f"{name}님 기준에 잘 맞는 후보 3가지를 골라봤어요. "
                    "아래 카드와 함께, 하나씩 간단히 소개해드릴게요."
                )
    
                for idx, p in enumerate(prods, start=1):
                    reason = generate_personalized_reason(p, mems, name).split("\n")[0]
                    msg = (
                        f"{idx}번 후보 **{p['name']}** (약 {p['price']:,}원대)\n"
                        f"- 주요 특징: {', '.join(p.get('tags', []))}\n"
                        f"- 왜 어울릴까요? {reason}"
                    )
                    ai_say(msg)
    
                ai_say(
                    "각 후보는 아래 카드 형태로도 정리해두었어요. "
                    "관심 가는 제품의 카드에서 **'자세히 질문하기'** 버튼을 누르시면, "
                    "그 제품에 대해 제가 채팅으로 더 자세히 안내해드릴게요.\n\n"
                    "최종적으로 마음에 드는 제품을 고르셨다면, 카드 하단의 "
                    "**'구매하러 가기'** 버튼을 눌러 구매를 진행하는 상황을 가정해볼게요.\n"
                    "*구매하러 가기는 자세히 질문하기를 거쳐야만 하단 버튼을 볼 수 있습니다"
                )
    
                st.rerun()
    
        # summary 외 단계에서는 안내 문구만
        if st.session_state.stage != "summary":
            st.info(
                "수정하실 기준이 있으면 아래 입력창에서 말씀해주세요.😊**\n"
                "또한 *종종 에이전트가 출력오류로 동일한 질문을 던질 수 있습니다. 그럴 땐 '추천해줘'를 입력해주세요!"
            )
        # ------------------------------------------------
        # 입력폼
        # ------------------------------------------------
        with st.form(key="chat_form", clear_on_submit=True):
            c1, c2 = st.columns([85, 15])
            with c1:
                st.text_input(
                    "msg",
                    key="user_input_text",
                    label_visibility="collapsed",
                    placeholder="메시지를 입력하세요. 응답에는 약 3-4초 소요될 수 있습니다",
                )
            with c2:
                if st.form_submit_button("전송"):
                    handle_input()
                    st.rerun()

        # ------------------------------------------------
        # 추천 / 상세 / 구매 단계  ← 반드시 SUMMARY 블록과 같은 깊이여야 함
        # ------------------------------------------------
        if st.session_state.stage in ["comparison", "product_detail", "purchase_decision"]:
            st.markdown("---")
    
            if st.session_state.stage == "product_detail":
                c1, c2 = st.columns([1, 4])
                with c1:
                    if st.button("목록으로(⬅️)"):
                        st.session_state.stage = "comparison"
                        st.session_state.selected_product = None
                        st.rerun()
    
            recommend_products_ui(st.session_state.nickname, st.session_state.memory)

        # ------------------------------------------------
        # 구매 결정 단계 완성 표시
        # ------------------------------------------------
        if st.session_state.stage == "purchase_decision" and st.session_state.final_choice:
            p = st.session_state.final_choice
            st.success(f"🎉 **{p['name']}** 구매를 결정하셨습니다! 이제 실험이 끝났습니다. 설문으로 돌아가주세요!")
            st.balloons()

# =========================================================
# 19. 라우팅
# =========================================================
if st.session_state.page == "context_setting":
    context_setting_page()
else:
    main_chat_interface()






