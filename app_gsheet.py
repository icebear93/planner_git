import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials
import random
import base64
import hashlib
import hmac

# ----------------- 기본 설정 -----------------
st.set_page_config(
    page_title="Jason 루틴 플랫폼 (GSheet)",
    page_icon="🎯",
    layout="wide",
)

def _decode_salt(s: str) -> bytes:
    try:
        return base64.b64decode(s)
    except Exception:
        return bytes.fromhex(s)

def _verify_password(password: str) -> bool:
    if "auth" not in st.secrets:
        return False
    auth = st.secrets["auth"]
    if "password_hash" not in auth or "salt" not in auth:
        return False
    iterations = int(auth.get("iterations", 200_000))
    salt = _decode_salt(auth["salt"])
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    expected = bytes.fromhex(auth["password_hash"])
    return hmac.compare_digest(derived, expected)

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if "auth" not in st.secrets or "password_hash" not in st.secrets["auth"] or "salt" not in st.secrets["auth"]:
    st.error("Secrets에 [auth] 설정이 필요합니다. (password_hash, salt, iterations)")
    st.stop()

if st.session_state.authenticated:
    with st.sidebar:
        if st.button("로그아웃"):
            st.session_state.authenticated = False
            st.rerun()
else:
    st.title("로그인")
    password = st.text_input("비밀번호", type="password")
    if st.button("로그인"):
        if _verify_password(password):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()

# ----------------- 커스텀 CSS -----------------
st.markdown(
    """
<style>
    .main .block-container { padding-top: 1.5rem; }
    [data-testid="stMetricValue"] { font-size: 1.8rem; font-weight: 700; }
    .motivation-box {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white; padding: 1.2rem; border-radius: 15px;
        text-align: center; font-size: 1.1rem; margin: 1rem 0;
    }
    .badge { display: inline-block; padding: 0.3rem 0.8rem;
             border-radius: 20px; font-weight: 600; margin: 0.2rem; }
    .badge-gold { background: linear-gradient(135deg, #f39c12, #e74c3c); color: white; }
    .badge-silver { background: linear-gradient(135deg, #bdc3c7, #95a5a6); color: white; }
    .badge-bronze { background: linear-gradient(135deg, #e67e22, #d35400); color: white; }
</style>
""",
    unsafe_allow_html=True,
)

# ----------------- 상수/스키마 -----------------
CONFIG_HEADERS = ["start_date", "auto_phase", "manual_phase", "target_exam"]
LOG_HEADERS = [
    "date",
    "phase",
    "day_type",
    "mode",
    "block",
    "done",
    "estimated_minutes",
    "energy",
    "focus",
    "note",
    "subject",
]
SUBJECT_HEADERS = ["name", "total_lectures", "completed_lectures", "active"]

PHASE_LABELS = {
    1: "1단계 – 출석 + 공부 모양",
    2: "2단계 – 0.5~1회 감각",
    3: "3단계 – 공부시간 증가",
    4: "4단계 – 완성형",
}

DAY_TYPE_LABELS = {"weekday": "평일", "sat": "토요일", "sun": "일요일"}
MODE_LABELS = {
    "normal": "정상 모드",
    "low": "저자극 모드 (10%)",
    "off": "OFF 모드",
}

DAILY_GRADE_HINT = "일일 등급 기준: S ≥ 4.6h, C ≥ 3.9h, B ≥ 3.1h, A ≥ 2.5h, 그 미만 D-"
WEEKLY_GRADE_HINT = "주간 등급 기준: S ≥ 32h, C ≥ 27h, B ≥ 22h, A ≥ 18h, 그 미만 D-"

DEFAULT_CONFIG = {
    "start_date": date.today().isoformat(),
    "auto_phase": True,
    "manual_phase": 1,
    "target_exam": "2027-01-01",
}

SHOW_EXCEL_TAB = True  # 엑셀 참고 탭 제거 시 False 또는 블록 삭제

# ----------------- GSheet 클라이언트 -----------------

def _parse_bool(value, default: bool = False) -> bool:
    if value is None or pd.isna(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "y", "t"):
        return True
    if s in ("false", "0", "no", "n", "f", ""):
        return False
    return default

def get_client():
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"])
    scoped = creds.with_scopes([
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(scoped)


def get_workbook():
    client = get_client()
    if "gsheet" not in st.secrets or "spreadsheet_url" not in st.secrets["gsheet"]:
        st.stop()
    return client.open_by_url(st.secrets["gsheet"]["spreadsheet_url"])


def ensure_worksheet(wb, name: str, headers: list):
    try:
        ws = wb.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=name, rows=100, cols=len(headers) + 5)
        ws.append_row(headers)
    # 헤더가 없으면 추가
    values = ws.get_all_values()
    if not values:
        ws.append_row(headers)
    elif values[0][: len(headers)] != headers:
        ws.insert_row(headers, 1)
    return ws

# ----------------- 저장/불러오기 -----------------

def load_config():
    wb = get_workbook()
    ws = ensure_worksheet(wb, "config", CONFIG_HEADERS)
    rows = ws.get_all_records()
    cfg = rows[0] if rows else DEFAULT_CONFIG.copy()
    # 기본값 보정
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    cfg["auto_phase"] = _parse_bool(cfg.get("auto_phase", DEFAULT_CONFIG["auto_phase"]), default=DEFAULT_CONFIG["auto_phase"])
    try:
        cfg["manual_phase"] = int(float(cfg.get("manual_phase", DEFAULT_CONFIG["manual_phase"])))
    except Exception:
        cfg["manual_phase"] = int(DEFAULT_CONFIG["manual_phase"])
    cfg["_start_date_obj"] = datetime.fromisoformat(str(cfg["start_date"])) .date()
    cfg["_target_exam_obj"] = datetime.fromisoformat(str(cfg["target_exam"])) .date()
    return cfg


def save_config(cfg: dict):
    wb = get_workbook()
    ws = ensure_worksheet(wb, "config", CONFIG_HEADERS)
    ws.clear()
    ws.append_row(CONFIG_HEADERS)
    row = [cfg.get(k, DEFAULT_CONFIG.get(k)) for k in CONFIG_HEADERS]
    ws.append_row(row)


def load_subjects():
    wb = get_workbook()
    ws = ensure_worksheet(wb, "subjects", SUBJECT_HEADERS)
    rows = ws.get_all_records()
    if not rows:
        return [{"name": "민법", "total_lectures": 220, "completed_lectures": 0, "active": True}]
    # 타입 보정
    for r in rows:
        try:
            r["total_lectures"] = int(float(r.get("total_lectures", 0) or 0))
        except Exception:
            r["total_lectures"] = 0
        try:
            r["completed_lectures"] = int(float(r.get("completed_lectures", 0) or 0))
        except Exception:
            r["completed_lectures"] = 0
        r["active"] = _parse_bool(r.get("active", True), default=True)
    return rows


def save_subjects(subjects: list):
    wb = get_workbook()
    ws = ensure_worksheet(wb, "subjects", SUBJECT_HEADERS)
    ws.clear()
    ws.append_row(SUBJECT_HEADERS)
    for s in subjects:
        ws.append_row([s.get(h, "") for h in SUBJECT_HEADERS])


def _normalize_plan_rows(rows: list, headers: list, int_fields=None, bool_fields=None) -> list:
    normalized = []
    int_fields = set(int_fields or [])
    bool_fields = set(bool_fields or [])
    for row in rows:
        clean = {h: row.get(h, "") for h in headers}
        for field in bool_fields:
            clean[field] = _parse_bool(clean.get(field, False), default=False)
        for field in int_fields:
            try:
                clean[field] = int(float(clean.get(field) or 0))
            except Exception:
                clean[field] = 0
        normalized.append(clean)
    return normalized


def load_plan_sheet(name: str, headers: list, defaults: list, int_fields=None, bool_fields=None) -> list:
    wb = get_workbook()
    ws = ensure_worksheet(wb, name, headers)
    rows = ws.get_all_records()
    if not rows:
        save_plan_sheet(name, headers, defaults)
        return _normalize_plan_rows(defaults, headers, int_fields, bool_fields)
    return _normalize_plan_rows(rows, headers, int_fields, bool_fields)


def save_plan_sheet(name: str, headers: list, rows: list):
    wb = get_workbook()
    ws = ensure_worksheet(wb, name, headers)
    ws.clear()
    ws.append_row(headers)
    if not rows:
        return
    out_rows = []
    for row in rows:
        out_rows.append([row.get(h, "") for h in headers])
    ws.append_rows(out_rows)


def load_plan_overview() -> list:
    return load_plan_sheet("plan_overview", PLAN_OVERVIEW_HEADERS, PLAN_OVERVIEW_DEFAULT)


def save_plan_overview(rows: list):
    save_plan_sheet("plan_overview", PLAN_OVERVIEW_HEADERS, rows)


def load_plan_weekly() -> list:
    return load_plan_sheet("plan_weekly", PLAN_WEEKLY_HEADERS, PLAN_WEEKLY_DEFAULT)


def save_plan_weekly(rows: list):
    save_plan_sheet("plan_weekly", PLAN_WEEKLY_HEADERS, rows)


def load_plan_friday() -> list:
    return load_plan_sheet(
        "plan_friday",
        PLAN_FRIDAY_HEADERS,
        PLAN_FRIDAY_DEFAULT,
        int_fields=["week"],
        bool_fields=["status"],
    )


def save_plan_friday(rows: list):
    save_plan_sheet("plan_friday", PLAN_FRIDAY_HEADERS, rows)


def load_plan_micro() -> list:
    return load_plan_sheet(
        "plan_micro",
        PLAN_MICRO_HEADERS,
        PLAN_MICRO_DEFAULT,
        bool_fields=["status"],
    )


def save_plan_micro(rows: list):
    save_plan_sheet("plan_micro", PLAN_MICRO_HEADERS, rows)


def load_plan_logic() -> list:
    return load_plan_sheet(
        "plan_logic",
        PLAN_LOGIC_HEADERS,
        PLAN_LOGIC_DEFAULT,
        int_fields=["round"],
        bool_fields=["status"],
    )


def save_plan_logic(rows: list):
    save_plan_sheet("plan_logic", PLAN_LOGIC_HEADERS, rows)


def load_plan_baking() -> list:
    return load_plan_sheet(
        "plan_baking",
        PLAN_BAKING_HEADERS,
        PLAN_BAKING_DEFAULT,
        bool_fields=["status"],
    )


def save_plan_baking(rows: list):
    save_plan_sheet("plan_baking", PLAN_BAKING_HEADERS, rows)


def load_log() -> pd.DataFrame:
    wb = get_workbook()
    ws = ensure_worksheet(wb, "log", LOG_HEADERS)
    rows = ws.get_all_records()
    if not rows:
        return pd.DataFrame(columns=LOG_HEADERS)
    df = pd.DataFrame(rows)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    for col in LOG_HEADERS:
        if col not in df.columns:
            df[col] = pd.NA
    if "done" in df.columns:
        df["done"] = df["done"].apply(lambda v: _parse_bool(v, default=False))
    if "estimated_minutes" in df.columns:
        df["estimated_minutes"] = pd.to_numeric(df["estimated_minutes"], errors="coerce").fillna(0).astype(int)
    if "phase" in df.columns:
        df["phase"] = pd.to_numeric(df["phase"], errors="coerce").fillna(0).astype(int)
    for _col in ["energy", "focus"]:
        if _col in df.columns:
            df[_col] = pd.to_numeric(df[_col], errors="coerce").astype("Int64")
    return df[LOG_HEADERS]


def save_log(df: pd.DataFrame):
    wb = get_workbook()
    ws = ensure_worksheet(wb, "log", LOG_HEADERS)
    ws.clear()
    ws.append_row(LOG_HEADERS)
    if df.empty:
        return
    # 날짜를 문자열로 변환
    out_df = df.copy()
    out_df["date"] = out_df["date"].astype(str)
    rows = out_df[LOG_HEADERS].fillna("").values.tolist()
    ws.append_rows(rows)

# ----------------- Phase / Week 계산 -----------------

def get_week_number(start_date: date, target_date: date) -> int:
    delta = (target_date - start_date).days
    return max(1, delta // 7 + 1)


def get_phase_by_week(week_num: int) -> int:
    if week_num <= 1:
        return 1
    elif week_num <= 3:
        return 2
    elif week_num <= 6:
        return 3
    else:
        return 4


def get_day_type(d: date) -> str:
    w = d.weekday()
    return "weekday" if w < 5 else ("sat" if w == 5 else "sun")


def get_week_range(d: date):
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)

# ----------------- 상세 시간표 -----------------

def get_detailed_schedule(phase: int, day_type: str, mode: str):
    schedule = []
    if mode == "off":
        return [("전일", "OFF 모드 (완전 휴식)", "rest", 0, "푹 쉬고 내일 복귀하세요")]

    if day_type == "weekday":
        schedule.append(("05:30", "기상 + 준비", "morning", 0, "물 한잔, 세수, 스트레칭"))
        schedule.append(("06:00-07:20", "출근 이동", "morning", 0, ""))
        if phase >= 1:
            schedule.append(("07:40-08:40", "☕ 아침 카페 출석", "study", 0, "카페 도착 = 오늘 50% 성공"))
        if phase >= 2:
            schedule.append(("07:40-08:10", "   └ 전날 인강 다시 보기", "study", 30, "표시해둔 구간만 복습"))
            schedule.append(("08:10-08:40", "   └ 복습용 문풀", "study", 30, "전날 강의 내용 5~7문제"))
        schedule.append(("09:00-18:00", "💼 회사", "work", 0, ""))
        schedule.append(("18:00-20:00", "퇴근 + 저녁", "rest", 0, ""))
        schedule.append(("20:00-20:45", "저녁 휴식", "rest", 0, "유튜브/게임 가능 (공부 시작 전까지만)"))
        if phase >= 1:
            schedule.append(("20:45", "🪑 저녁 출석 (앉기)", "study", 0, "의자에 앉는 순간 70% 성공"))
        if phase == 1:
            schedule.append(("20:45-21:30", "   └ 인강 틀어놓기/책 펴놓기", "study", 45, "이해 0%여도 상관없음, 모양만"))
            schedule.append(("21:30-22:00", "🏋️ 운동 30분", "exercise", 0, "로잉/유산소"))
            schedule.append(("22:00-22:20", "🚿 샤워", "exercise", 0, "모드 전환 의식"))
            schedule.append(("22:20-23:00", "책상 앞 유지", "study", 0, "민법책 펼쳐보기, 자서전 읽기, 멍"))
        elif phase == 2:
            schedule.append(("20:45-21:30", "📚 인강 1강", "study", 45, "제대로 들어보려고 노력"))
            schedule.append(("21:30-22:00", "🏋️ 운동 30분", "exercise", 0, ""))
            schedule.append(("22:00-22:20", "🚿 샤워", "exercise", 0, ""))
            schedule.append(("22:20-23:00", "📚 인강 이어서 or 복습", "study", 40, "2번째 강의 시작해보기"))
        elif phase == 3:
            schedule.append(("20:45-21:30", "📚 인강 1강", "study", 45, ""))
            schedule.append(("21:30-21:50", "✏️ 1차 문풀", "study", 20, "방금 들은 1강 관련 4-6문제"))
            schedule.append(("21:50-22:20", "🏋️ 운동 30분", "exercise", 0, ""))
            schedule.append(("22:20-22:35", "🚿 샤워", "exercise", 0, ""))
            schedule.append(("22:35-23:20", "📚 인강 2강", "study", 45, ""))
            schedule.append(("23:20-23:40", "📖 복습 + 정리", "study", 20, "오늘 내용 핵심 메모"))
        elif phase == 4:
            schedule.append(("20:45-21:30", "📚 인강 1강", "study", 45, "오늘 3강 중 1강"))
            schedule.append(("21:30-21:50", "✏️ 1차 문풀", "study", 20, "1강 관련 4-6문제"))
            schedule.append(("21:50-22:20", "🏋️ 운동 30분", "exercise", 0, "월/수/금 or 화/목"))
            schedule.append(("22:20-22:35", "🚿 샤워 15분", "exercise", 0, "공부 모드 스위치 ON"))
            schedule.append(("22:35-23:20", "📚 인강 2강", "study", 45, ""))
            schedule.append(("23:20-23:35", "✏️ 2차 문풀", "study", 15, "2강 관련 3-5문제"))
            schedule.append(("23:35-00:20", "📚 인강 3강", "study", 45, "피곤하면 틀어놓기 모드 허용"))
            schedule.append(("00:20-00:40", "✏️ 마감 문풀 + 정리", "study", 20, "핵심 3-5줄 메모, 내일 복습 포인트 표시"))
        schedule.append(("00:40-01:00", "자유시간 + 취침 준비", "rest", 0, ""))
        schedule.append(("01:00", "💤 취침", "rest", 0, "05:30 기상 리듬 유지"))
    else:
        schedule.append(("09:00-09:30", "기상 + 씻기 + 정리", "morning", 0, ""))
        if phase >= 1:
            schedule.append(("09:30-10:30", "☕ 아침 복습 블록", "study", 60, "전날/한 주 누적 복습"))
        if phase == 1:
            schedule.append(("10:30-12:00", "📚 인강 틀기", "study", 60, "1강만 끝나도 대성공, 모양 유지"))
            schedule.append(("12:00-13:00", "점심 + 휴식", "rest", 0, ""))
            schedule.append(("13:00-15:00", "📚 인강 or 유지", "study", 60, "한 블록만 앉아있어도 성공"))
        elif phase == 2:
            schedule.append(("10:30-12:00", "📚 인강 1~2강", "study", 90, ""))
            schedule.append(("12:00-13:00", "점심 + 휴식", "rest", 0, ""))
            schedule.append(("13:00-15:00", "📚 인강 이어서", "study", 90, "하루 3-4강 목표"))
            schedule.append(("15:30-17:30", "📖 가벼운 문풀/복습", "study", 60, ""))
        elif phase == 3:
            schedule.append(("10:30-12:00", "📚 오전 인강 2강", "study", 90, ""))
            schedule.append(("12:00-13:00", "점심", "rest", 0, ""))
            schedule.append(("13:00-15:00", "📚 오후 전반 인강 2강", "study", 90, ""))
            schedule.append(("15:00-16:00", "✏️ 문풀 1차", "study", 60, "오전 4강 관련 15-20문제"))
            schedule.append(("16:00-17:30", "📚 오후 후반 인강", "study", 90, ""))
        elif phase == 4:
            schedule.append(("09:30-10:30", "☕ 아침 복습", "study", 60, "복습용 문풀 10-15문제"))
            schedule.append(("10:30-12:00", "📚 오전 인강 2강", "study", 90, ""))
            schedule.append(("12:00-13:00", "점심 + 휴식", "rest", 0, "산책 10분"))
            schedule.append(("13:00-14:30", "📚 오후 인강 2강", "study", 90, "이 시점 4/6강 완료"))
            schedule.append(("14:30-15:30", "✏️ 문풀 1차", "study", 60, "오전 4강 관련 15-25문제"))
            schedule.append(("15:30-17:00", "📚 오후 후반 인강 2강", "study", 90, "6강 마무리"))
            schedule.append(("17:00-18:00", "저녁 + 휴식", "rest", 0, ""))
            schedule.append(("18:00-19:30", "✏️ 문풀 2차", "study", 90, "하루 전체 + 주간 누적 20-30문제"))
            schedule.append(("19:30-20:00", "📝 정리 + 내일 준비", "study", 30, "핵심 메모, 내일 복습 포인트"))
        schedule.append(("20:00 이후", "자유시간 + 산책", "rest", 0, ""))
    return schedule


def get_checkable_blocks(phase: int, day_type: str, mode: str):
    schedule = get_detailed_schedule(phase, day_type, mode)
    blocks = []
    for item in schedule:
        time, name, category, minutes, desc = item
        if category in ["study", "exercise"] and minutes >= 0:
            clean_name = name.strip()
            if clean_name.startswith("└"):
                clean_name = clean_name[1:].strip()
            blocks.append((clean_name, minutes, desc))
    return blocks

# ----------------- 동기부여 메시지 -----------------

def get_logged_day_context(log_df: pd.DataFrame, target_date: date):
    if log_df.empty:
        return None
    if "date" not in log_df.columns:
        return None
    mask = log_df["date"] == target_date
    if not mask.any():
        return None
    row = log_df[mask].iloc[0]
    try:
        return {"phase": int(row["phase"]), "day_type": row["day_type"], "mode": row["mode"]}
    except Exception:
        return None

MOTIVATION_MESSAGES = {
    "streak_high": [
        "🔥 {streak}일 연속! 루틴이 뼛속에 새겨지는 중!",
        "💪 {streak}일째! 이게 진짜 실력이야!",
    ],
    "streak_start": ["👊 {streak}일째! 좋은 시작이야!", "🌱 습관이 자라는 중!"],
    "low_mode": ["🌿 10%라도 0%보다 10배야!", "☘️ 저자극도 출석이야!"],
    "default": [
        "📚 앉았어! 이미 50% 성공!",
        "🎯 출석이 곧 실력!",
        "💡 앉기만 하면 공부량은 자동으로 늘어나!",
    ],
}


def get_motivation_message(streak: int, mode: str = "normal"):
    if mode == "low":
        return random.choice(MOTIVATION_MESSAGES["low_mode"])
    if streak >= 7:
        return random.choice(MOTIVATION_MESSAGES["streak_high"]).format(streak=streak)
    if streak >= 2:
        return random.choice(MOTIVATION_MESSAGES["streak_start"]).format(streak=streak)
    return random.choice(MOTIVATION_MESSAGES["default"])

# ----------------- 등급 -----------------

def get_daily_grade(hours: float) -> str:
    if hours < 2.5:
        return "D-"
    elif hours < 3.1:
        return "A"
    elif hours < 3.9:
        return "B"
    elif hours < 4.6:
        return "C"
    else:
        return "S"

# ----------------- 과목/진도 계산 -----------------

def get_lecture_increment(block_name: str) -> int:
    name = str(block_name)
    if "인강 3강" in name:
        return 1
    if "인강 2강" in name:
        return 2 if ("오전 인강 2강" in name or "오후 인강 2강" in name or "후반 인강 2강" in name) else 1
    if "인강 1강" in name or "인강 1~2강" in name or "인강 이어서" in name:
        return 1
    return 0


def lecture_credits_from_rows(rows: pd.DataFrame) -> int:
    if rows.empty:
        return 0
    return sum(get_lecture_increment(b) for b in rows.loc[rows["done"] == True, "block"])


def compute_subject_progress(log_df: pd.DataFrame) -> dict:
    progress = {}
    if log_df.empty or "subject" not in log_df.columns:
        return progress
    for subj, rows in log_df.groupby("subject"):
        if pd.isna(subj) or subj == "":
            continue
        progress[subj] = lecture_credits_from_rows(rows)
    return progress


def sync_subjects_with_log(log_df: pd.DataFrame, subjects: list) -> list:
    if not subjects:
        return subjects
    progress = compute_subject_progress(log_df)
    changed = False
    for s in subjects:
        name = s.get("name")
        if name in progress:
            new_val = max(progress[name], s.get("completed_lectures", 0))
            if new_val != s.get("completed_lectures", 0):
                s["completed_lectures"] = new_val
                changed = True
    if changed:
        save_subjects(subjects)
    return subjects

# ----------------- 배지 -----------------

def get_badges(log_df, subjects, streak):
    badges = []
    if streak >= 30:
        badges.append(("🏆 30일 연속", "gold"))
    elif streak >= 14:
        badges.append(("🥈 14일 연속", "silver"))
    elif streak >= 7:
        badges.append(("🥉 7일 연속", "bronze"))
    for s in subjects:
        if s["completed_lectures"] >= s["total_lectures"]:
            badges.append((f"📚 {s['name']} 완주!", "gold"))
        elif s["completed_lectures"] >= s["total_lectures"] * 0.5:
            badges.append((f"📖 {s['name']} 50%", "silver"))
    return badges

# ----------------- 히트맵 -----------------

def render_heatmap(log_df, weeks=12):
    today = date.today()
    start = today - timedelta(weeks=weeks, days=today.weekday())
    daily_data = {}
    if not log_df.empty:
        for d in pd.date_range(start, today):
            mask = log_df["date"] == d.date()
            if mask.any():
                daily_data[d.date()] = log_df[mask]["estimated_minutes"].sum()
    cols = st.columns(weeks)
    for w in range(weeks):
        ws = start + timedelta(weeks=w)
        with cols[w]:
            for d in range(7):
                cd = ws + timedelta(days=d)
                if cd > today:
                    c = "#1a1a1a"
                elif cd in daily_data:
                    m = daily_data[cd]
                    c = "#00d4aa" if m >= 240 else "#00a884" if m >= 120 else "#007a5e" if m >= 30 else "#004d3d"
                else:
                    c = "#2d3436"
                st.markdown(
                    f'<div style="width:12px;height:12px;background:{c};'
                    f'border-radius:2px;margin:1px;display:inline-block;" '
                    f'title="{cd}"></div>',
                    unsafe_allow_html=True,
                )

# ----------------- 세션 초기화 -----------------
if "config" not in st.session_state:
    st.session_state.config = load_config()
if "log_df" not in st.session_state:
    st.session_state.log_df = load_log()
if "subjects" not in st.session_state:
    st.session_state.subjects = load_subjects()
if SHOW_EXCEL_TAB and "plan_overview" not in st.session_state:
    st.session_state.plan_overview = load_plan_overview()
if SHOW_EXCEL_TAB and "plan_weekly" not in st.session_state:
    st.session_state.plan_weekly = load_plan_weekly()
if SHOW_EXCEL_TAB and "plan_friday" not in st.session_state:
    st.session_state.plan_friday = load_plan_friday()
if SHOW_EXCEL_TAB and "plan_micro" not in st.session_state:
    st.session_state.plan_micro = load_plan_micro()
if SHOW_EXCEL_TAB and "plan_logic" not in st.session_state:
    st.session_state.plan_logic = load_plan_logic()
if SHOW_EXCEL_TAB and "plan_baking" not in st.session_state:
    st.session_state.plan_baking = load_plan_baking()

config = st.session_state.config
log_df = st.session_state.log_df
subjects = sync_subjects_with_log(log_df, st.session_state.subjects)
st.session_state.subjects = subjects
today = date.today()

# ----------------- 사이드바 -----------------
with st.sidebar:
    st.markdown("## ⚙️ 설정")
    selected_date = st.date_input("📅 작업할 날짜", value=today)

    saved_ctx = get_logged_day_context(log_df, selected_date)
    use_saved_ctx = False
    if saved_ctx:
        st.info(
            f"📌 이 날짜에는 기록이 있습니다: Phase {saved_ctx['phase']}, "
            f"{DAY_TYPE_LABELS.get(saved_ctx['day_type'], saved_ctx['day_type'])}, "
            f"{MODE_LABELS.get(saved_ctx['mode'], saved_ctx['mode'])}"
        )
        use_saved_ctx = st.checkbox("기록된 설정 우선 사용", True, key="use_saved_ctx")

    selected_week = get_week_number(config["_start_date_obj"], selected_date)
    selected_phase_auto = get_phase_by_week(selected_week)

    day_type_options = list(DAY_TYPE_LABELS.keys())
    default_day_type = get_day_type(selected_date)
    if saved_ctx:
        default_day_type = saved_ctx.get("day_type", default_day_type)
    day_type_index = day_type_options.index(default_day_type) if default_day_type in day_type_options else 0
    day_type = st.selectbox(
        "요일 타입",
        options=day_type_options,
        index=day_type_index,
        format_func=lambda x: DAY_TYPE_LABELS[x],
        disabled=bool(saved_ctx and use_saved_ctx),
    )

    if saved_ctx and use_saved_ctx:
        effective_phase = int(saved_ctx["phase"])
        st.info(f"기록된 단계 사용: {PHASE_LABELS[effective_phase]}")
    elif config["auto_phase"]:
        phase_options = list(PHASE_LABELS.keys())
        effective_phase = st.selectbox(
            "단계 (Phase)",
            options=phase_options,
            index=phase_options.index(selected_phase_auto),
            format_func=lambda x: PHASE_LABELS[x],
            help=f"자동 추천: {selected_week}주차 → {selected_phase_auto}단계",
        )
    else:
        effective_phase = config["manual_phase"]
        st.info(f"수동 고정: {PHASE_LABELS[effective_phase]}")

    mode_options = list(MODE_LABELS.keys())
    default_mode = saved_ctx.get("mode") if saved_ctx else mode_options[0]
    if default_mode not in mode_options:
        default_mode = mode_options[0]
    mode = st.radio(
        "모드",
        mode_options,
        index=mode_options.index(default_mode),
        format_func=lambda x: MODE_LABELS[x],
        horizontal=True,
        disabled=bool(saved_ctx and use_saved_ctx),
    )

    st.markdown("---")
    st.caption(f"📅 {selected_date} | {selected_week}주차")
    phase_emoji = ["", "🟢", "🟡", "🟠", "🔴"][effective_phase]
    st.markdown(f"**{phase_emoji} {PHASE_LABELS[effective_phase]}**")

# ----------------- 출석 streak -----------------
unique_dates = sorted(log_df["date"].unique(), reverse=True) if not log_df.empty else []
streak = 0
for d in unique_dates:
    if d > today:
        continue
    day_rows = log_df[log_df["date"] == d]
    if day_rows.empty:
        break
    if ((day_rows["done"] == True) & (day_rows["block"] != "OFF")).any():
        streak += 1
    else:
        break

# ----------------- 메인 -----------------
st.markdown("# 🎯 Jason 루틴 플랫폼 (GSheet)")

tab_labels = ["🏠 대시보드", "✅ 오늘 루틴", "📚 과목 관리", "📊 분석", "📜 철학", "⚙️ 설정"]
if SHOW_EXCEL_TAB:
    tab_labels.append("📎 엑셀 플랜")
tabs = st.tabs(tab_labels)
tab_dashboard, tab_routine, tab_subjects, tab_analysis, tab_philosophy, tab_settings = tabs[:6]
tab_excel = tabs[6] if SHOW_EXCEL_TAB else None

# ==================== 대시보드 ====================
with tab_dashboard:
    st.markdown(f"## 📊 {selected_date} 현황")
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("🔥 연속 출석", f"{streak}일")
    with col2:
        checkable = get_checkable_blocks(effective_phase, day_type, mode)
        mask_today = log_df["date"] == selected_date if not log_df.empty else pd.Series([False])
        today_done = log_df[mask_today & (log_df["done"] == True)].shape[0] if not log_df.empty else 0
        total = max(len(checkable), 1)
        progress = int((today_done / total) * 100)
        st.metric("📈 진행률", f"{progress}%")
    with col3:
        today_min = log_df[mask_today]["estimated_minutes"].sum() if not log_df.empty else 0
        st.metric("⏱️ 공부시간", f"{today_min // 60}시간 {today_min % 60}분")
    with col4:
        today_grade = get_daily_grade(today_min / 60 if today_min else 0)
        st.metric("🏅 일일 등급", today_grade, help=DAILY_GRADE_HINT)
    with col5:
        phase_emoji = ["", "🟢", "🟡", "🟠", "🔴"][effective_phase]
        st.metric("🎯 Phase", f"{phase_emoji} {effective_phase}단계")

    with st.expander("⏱️ 공부시간 계산 방법"):
        st.markdown(
            """
        - **블록별 예상 시간**을 합산합니다
        - 체크한 블록의 `예상 시간`만 카운트됩니다
        - 출석/운동/샤워 등은 0분으로 계산 (공부시간 아님)
        - 인강 1강 = 45분, 문풀 = 15~20분 등 기준
        """
        )

    st.markdown(
        f"""
    <div class="motivation-box">{get_motivation_message(streak, mode)}</div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("### 📚 과목별 진도")
    active_subj = [s for s in subjects if s.get("active", True)]
    if active_subj:
        cols = st.columns(len(active_subj))
        for i, s in enumerate(active_subj):
            with cols[i]:
                p = int((s["completed_lectures"] / s["total_lectures"]) * 100) if s["total_lectures"] > 0 else 0
                st.markdown(f"**{s['name']}**")
                st.progress(p / 100)
                st.caption(f"{s['completed_lectures']}/{s['total_lectures']}강 ({p}%)")
    else:
        st.info("📚 '과목 관리'에서 과목을 추가하세요!")

    badges = get_badges(log_df, subjects, streak)
    if badges:
        st.markdown("### 🏆 배지")
        st.markdown(
            " ".join([f'<span class="badge badge-{t}">{n}</span>' for n, t in badges]),
            unsafe_allow_html=True,
        )

    st.markdown("### 📅 12주 출석 히트맵")
    render_heatmap(log_df, 12)
    st.caption("💚 진할수록 공부 많이 함")

# ==================== 오늘 루틴 ====================
with tab_routine:
    st.markdown(f"## ✅ {selected_date} 루틴")
    phase_desc = {
        1: "🟢 **1단계**: 앉기만 해도 성공! 인강 틀어놓기/책 펴놓기 허용",
        2: "🟡 **2단계**: 하루 1~3강 도전. 힘들면 틀어놓기 모드 OK",
        3: "🟠 **3단계**: 아침 복습 + 저녁 인강 흐름 자리잡는 구간",
        4: "🔴 **4단계(완성형)**: 평일 3강+문풀, 주말 6강+문풀",
    }
    st.info(phase_desc[effective_phase])

    schedule = get_detailed_schedule(effective_phase, day_type, mode)
    mask_today = log_df["date"] == selected_date if not log_df.empty else pd.Series([False])
    today_existing = log_df[mask_today] if not log_df.empty else pd.DataFrame()

    st.markdown("### 📚 오늘 공부 과목")
    active_subj = [s for s in subjects if s.get("active", True)]
    subject_options = [s["name"] for s in active_subj]
    prev_subject = None
    if not today_existing.empty and today_existing["subject"].notna().any():
        prev_subject = str(today_existing["subject"].dropna().iloc[0])
    subj_index = subject_options.index(prev_subject) if prev_subject in subject_options else 0 if subject_options else 0
    selected_subject = st.selectbox(
        "기록에 남길 과목 (공부 블록에만 적용)",
        options=subject_options if subject_options else ["(과목 없음: 과목 관리에서 추가)"],
        index=subj_index if subject_options else 0,
        disabled=not bool(subject_options),
        key="routine_subject",
    )

    checkbox_states = {}
    block_meta = {}
    for time, name, category, minutes, desc in schedule:
        clean_name = name.strip()
        if clean_name.startswith("└"):
            clean_name = clean_name[1:].strip()
        block_meta[clean_name] = {"minutes": minutes, "category": category}
        cat_colors = {"morning": "🌅", "study": "📚", "exercise": "💪", "work": "💼", "rest": "😴"}
        emoji = cat_colors.get(category, "")
        if category in ["study", "exercise"]:
            prev = False
            if not today_existing.empty:
                prev_rows = today_existing[today_existing["block"] == clean_name]
                if not prev_rows.empty:
                    prev = bool(prev_rows.iloc[-1]["done"])
            time_label = f" [{minutes}분]" if minutes > 0 else ""
            desc_label = f" - {desc}" if desc else ""
            checkbox_states[clean_name] = st.checkbox(
                f"**{time}** {name}{time_label}{desc_label}",
                value=prev,
                key=f"cb_{clean_name}",
            )
        else:
            st.markdown(
                f"<div style='color:#888; padding:0.3rem 0;'>{emoji} **{time}** {name}</div>",
                unsafe_allow_html=True,
            )

    checkable = get_checkable_blocks(effective_phase, day_type, mode)
    total_possible = sum([m for _, m, _ in checkable])
    st.markdown(
        f"---\n**📊 체크 시 예상 공부시간: {total_possible}분 ({total_possible//60}시간 {total_possible%60}분)**"
    )

    st.markdown("### 🧠 컨디션")
    prev_energy, prev_focus, prev_note = 3, 3, ""
    if not today_existing.empty:
        if today_existing["energy"].notna().any():
            prev_energy = int(today_existing["energy"].dropna().iloc[0])
        if today_existing["focus"].notna().any():
            prev_focus = int(today_existing["focus"].dropna().iloc[0])
        if today_existing["note"].notna().any():
            prev_note = str(today_existing["note"].dropna().iloc[0])
    c1, c2 = st.columns(2)
    with c1:
        energy = st.slider("에너지 💪", 1, 5, prev_energy)
    with c2:
        focus = st.slider("집중도 🎯", 1, 5, prev_focus)
    note = st.text_area("한 줄 메모", prev_note, height=60, placeholder="오늘 느낀 점...")

    if st.button("💾 저장하기", type="primary"):
        log_df = log_df[~(log_df["date"] == selected_date)]
        if mode == "off":
            new_rows = [
                {
                    "date": selected_date,
                    "phase": effective_phase,
                    "day_type": day_type,
                    "mode": mode,
                    "block": "OFF",
                    "done": True,
                    "estimated_minutes": 0,
                    "energy": energy,
                    "focus": focus,
                    "note": note,
                    "subject": pd.NA,
                }
            ]
        else:
            new_rows = []
            for block, done in checkbox_states.items():
                meta = block_meta.get(block, {})
                est_min = meta.get("minutes", 0)
                subj_val = selected_subject if meta.get("category") == "study" and subject_options else pd.NA
                new_rows.append(
                    {
                        "date": selected_date,
                        "phase": effective_phase,
                        "day_type": day_type,
                        "mode": mode,
                        "block": block,
                        "done": bool(done),
                        "estimated_minutes": est_min if done else 0,
                        "energy": energy,
                        "focus": focus,
                        "note": note,
                        "subject": subj_val,
                    }
                )
        if new_rows:
            log_df = pd.concat([log_df, pd.DataFrame(new_rows)], ignore_index=True)
        st.session_state.log_df = log_df
        subjects = sync_subjects_with_log(log_df, subjects)
        st.session_state.subjects = subjects
        save_log(log_df)
        st.success("✅ 저장 완료!")
        st.rerun()

# ==================== 과목 관리 ====================
with tab_subjects:
    st.markdown("## 📚 과목 관리")
    st.caption("강의 수는 유동적으로 변경 가능, 여러 과목 동시 진행 OK")
    for idx, s in enumerate(subjects):
        with st.expander(f"📖 {s['name']} ({s['completed_lectures']}/{s['total_lectures']}강)", expanded=True):
            c1, c2, c3 = st.columns([2, 1, 1])
            with c1:
                nn = st.text_input("과목명", s["name"], key=f"sn_{idx}")
            with c2:
                nt = st.number_input("총 강의 수", value=s["total_lectures"], min_value=1, key=f"st_{idx}")
            with c3:
                nc = st.number_input(
                    "완료 강의 수", value=s["completed_lectures"], min_value=0, max_value=nt, key=f"sc_{idx}"
                )
            c4, c5 = st.columns(2)
            with c4:
                na = st.checkbox("활성화", s.get("active", True), key=f"sa_{idx}")
            with c5:
                if st.button("🗑️ 삭제", key=f"sd_{idx}"):
                    subjects.pop(idx)
                    save_subjects(subjects)
                    st.session_state.subjects = subjects
                    st.rerun()
            if nn != s["name"] or nt != s["total_lectures"] or nc != s["completed_lectures"] or na != s.get("active", True):
                subjects[idx] = {
                    "name": nn,
                    "total_lectures": nt,
                    "completed_lectures": nc,
                    "active": na,
                }
                save_subjects(subjects)
                st.session_state.subjects = subjects
    st.markdown("---\n### ➕ 새 과목")
    c1, c2 = st.columns(2)
    with c1:
        new_name = st.text_input("과목명", key="new_sn")
    with c2:
        new_total = st.number_input("총 강의 수", value=100, min_value=1, key="new_st")
    if st.button("➕ 추가", type="primary"):
        if new_name:
            subjects.append(
                {
                    "name": new_name,
                    "total_lectures": new_total,
                    "completed_lectures": 0,
                    "active": True,
                }
            )
            save_subjects(subjects)
            st.session_state.subjects = subjects
            st.success(f"✅ '{new_name}' 추가됨")
            st.rerun()

# ==================== 분석 ====================
with tab_analysis:
    st.markdown("## 📊 분석")
    if log_df.empty:
        st.info("기록이 없습니다. 루틴부터 시작하세요!")
    else:
        st.markdown("### 📆 주간 요약")
        week_ref = st.date_input("주 선택", today, key="wa")
        ws, we = get_week_range(week_ref)
        mask = (log_df["date"] >= ws) & (log_df["date"] <= we)
        wd = log_df[mask]
        if wd.empty:
            st.write("이 주에 기록 없음")
        else:
            tm = wd["estimated_minutes"].sum()
            th = round(tm / 60, 1)
            grade = "D-" if th < 18 else "A" if th < 22 else "B" if th < 27 else "C" if th < 32 else "S"
            c1, c2 = st.columns(2)
            with c1:
                st.metric("주간 공부시간", f"{th}h")
            with c2:
                st.metric("주간 등급", f"{grade}", help=WEEKLY_GRADE_HINT)
        st.markdown("---\n### 📈 장기 추세")
        ds = log_df.groupby("date")["estimated_minutes"].sum().reset_index()
        ds["hours"] = ds["estimated_minutes"] / 60
        st.line_chart(ds.set_index("date")["hours"], height=200)
        l7 = today - timedelta(days=6)
        r = ds[(ds["date"] >= l7) & (ds["date"] <= today)]
        avg7 = r["hours"].mean() if not r.empty else 0
        c1, c2 = st.columns(2)
        with c1:
            st.metric("최근 7일 평균", f"{avg7:.1f}h/일")
        with c2:
            st.metric("연속 출석", f"{streak}일")

with tab_philosophy:
    st.markdown(
        """
## 📜 Jason 루틴 철학

### 🎯 목표
- **1차**: 2027년 변리사 1차 합격
- **바로 앞**: 2026년 안 무너지는 루틴 완성

---

### ⚖️ 꾸준함의 정의 (헌법 7조)
1. 매일 100% 채우는 것 ≠ 꾸준함
2. 사람은 원래 들쑥날쑥 (10%/80%/0%)
3. **매일 조금이라도** 하는 사람이 이김
4. 핵심 = **양 X, 출석 O**
5. 출석 = 정해진 시간에 앉기
6. 앉기만 하면 공부량은 **자동 증가**
7. **10%라도 하면 루틴 붕괴 X**

---

### 🧱 5단계 시스템
| 단계 | 내용 |
|------|------|
| 1 | 출석 시스템 - 07:40 카페, 20:30 저녁 |
| 2 | 인강 사이클 - 저녁 진도, 아침 복습 |
| 3 | 10% 규칙 - 망한 날도 최소 수행 |
| 4 | 적응기→완성기 (2주→6주→이후) |
| 5 | 체력 시스템 - 운동+샤워 |

---

### 🚦 모드별 규칙
- **정상**: 풀 스케줄
- **저자극**: 틀어놓기 OK (유튜브 ❌)
- **OFF**: 완전 휴식 (2-3주에 1번만)

---

> **"공부는 못해도 루틴은 깬 적 없다."**
"""
    )

if SHOW_EXCEL_TAB and tab_excel is not None:
    with tab_excel:
        st.markdown("## 📎 엑셀 플랜 확인용 탭")
        st.caption("엑셀 내용을 그대로 확인하기 위한 전용 탭입니다. 제거하려면 SHOW_EXCEL_TAB=False 또는 이 블록 삭제")

        st.markdown("### 🧭 Overview")
        st.dataframe(pd.DataFrame(st.session_state.plan_overview), use_container_width=True)

        st.markdown("### 🗓️ Weekly Timeblocks")
        st.dataframe(pd.DataFrame(st.session_state.plan_weekly), use_container_width=True)

        st.markdown("### 🔁 Friday Rotation")
        st.dataframe(pd.DataFrame(st.session_state.plan_friday), use_container_width=True)

        st.markdown("### 📆 12-Week Micro Plan")
        st.dataframe(pd.DataFrame(st.session_state.plan_micro), use_container_width=True)

        st.markdown("### 🎧 Logic Quick Checklist")
        st.dataframe(pd.DataFrame(st.session_state.plan_logic), use_container_width=True)

        st.markdown("### 🧁 Baking Quick Checklist")
        st.dataframe(pd.DataFrame(st.session_state.plan_baking), use_container_width=True)

with tab_settings:
    st.markdown("## ⚙️ 설정")

    c1, c2 = st.columns(2)
    with c1:
        new_start = st.date_input("루틴 시작일", config["_start_date_obj"], key="ss")
        new_target = st.date_input("목표 시험일", config["_target_exam_obj"], key="st_main")
    with c2:
        auto_flag = st.checkbox("Phase 자동 전환", config["auto_phase"], key="saf")
        manual_phase_default = int(config.get("manual_phase", 1))
        if manual_phase_default not in PHASE_LABELS:
            manual_phase_default = 1
        mp = st.selectbox(
            "수동 고정 단계",
            list(PHASE_LABELS.keys()),
            index=list(PHASE_LABELS.keys()).index(manual_phase_default),
            format_func=lambda x: PHASE_LABELS[x],
            key="smp",
        )

    if st.button("💾 설정 저장", type="primary"):
        config.update(
            {
                "start_date": new_start.isoformat(),
                "target_exam": new_target.isoformat(),
                "auto_phase": auto_flag,
                "manual_phase": mp,
                "_start_date_obj": new_start,
                "_target_exam_obj": new_target,
            }
        )
        st.session_state.config = config
        save_config(config)
        st.success("✅ 저장 완료!")

    st.markdown("---\n### 📂 데이터 저장소")
    try:
        sheet_url = st.secrets["gsheet"]["spreadsheet_url"]
    except Exception:
        sheet_url = "(secrets에 spreadsheet_url 없음)"
    st.code(f"스프레드시트: {sheet_url}\n시트: config / log / subjects")
