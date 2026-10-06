import streamlit as st
from supabase import create_client

from recommender import GENRES, Recommender

st.set_page_config(page_title="다음책", page_icon="📚", layout="centered")


def secret(name, default=None):
    try:
        return st.secrets[name]
    except Exception:
        return default


# ---------- 로그인 ----------
# Secrets의 [USERS] 표에 "아이디 = 비밀번호"로 등록된 사람만 들어올 수 있음.
# 사람마다 아이디가 달라서 기록·추천이 서로 섞이지 않음.
try:
    USERS = {str(k): str(v) for k, v in dict(secret("USERS") or {}).items()}
except Exception:
    USERS = {}

if not st.session_state.get("user_id"):
    st.title("📚 다음책")
    if not USERS:
        st.error("등록된 사용자가 없어요. Secrets에 [USERS]를 추가해 주세요.")
        st.stop()
    uid = st.text_input("아이디").strip()
    pw = st.text_input("비밀번호", type="password")
    if st.button("들어가기", type="primary"):
        if uid in USERS and USERS[uid] == pw:
            st.session_state.user_id = uid
            st.rerun()
        else:
            st.error("아이디나 비밀번호가 달라요.")
    st.stop()

USER_ID = st.session_state.user_id


@st.cache_resource
def get_sb():
    return create_client(secret("SUPABASE_URL"), secret("SUPABASE_KEY"))


def get_rec(progress=None, config=None, exclude=None):
    return Recommender(get_sb(), secret("KAKAO_KEY"), secret("NARU_KEY"),
                       user_id=USER_ID, config=config, progress=progress, exclude=exclude)


@st.cache_data(ttl=3600, show_spinner=False)
def search_books(q):
    return get_rec().kakao_search(q)


RATINGS = [None] + [x / 2 for x in range(1, 11)]


def fmt_rating(r):
    return "별점 없음" if r is None else f"★ {r:g}"


def book_header(col, b):
    col.markdown(f"**{b.get('title') or ''}**")
    meta = " · ".join(x for x in [b.get("authors"), b.get("publisher")] if x)
    if meta:
        col.caption(meta)


def thumb(col, b):
    if b.get("thumbnail"):
        col.image(b["thumbnail"], width=80)
    else:
        col.markdown("📕")


st.session_state.setdefault("results", [])
st.session_state.setdefault("warns", [])
st.session_state.setdefault("hidden", set())
st.session_state.setdefault("shown", {})  # 이번 접속에서 이미 추천받은 책 {isbn: 책}

h1, h2 = st.columns([4, 1])
h1.title("📚 다음책")
h2.caption(f"{USER_ID}님")
if h2.button("로그아웃"):
    st.session_state.clear()
    st.rerun()
tab_rec, tab_add, tab_lib, tab_want, tab_no = st.tabs(
    ["추천받기", "책 기록하기", "내 서재", "읽고 싶은 책", "관심 없는 책"])

# ---------- 추천받기 ----------
with tab_rec:
    genres = st.multiselect("장르 (비워두면 전체)", list(GENRES), placeholder="장르를 골라주세요")
    balanced = True
    if len(genres) >= 2:
        balanced = st.radio("방식", ["골고루", "내 취향 우선"], horizontal=True) == "골고루"
    with st.expander("세부 설정"):
        w_content = st.slider("책 소개 비중 (나머지는 대출 데이터)", 0.0, 1.0, 0.5, 0.1)
        max_author = st.slider("한 작가당 최대 권수", 1, 5, 2)
    fresh = st.toggle("이번에 이미 본 책은 빼고 새 책으로", value=True,
                      help="끄면 다시 눌렀을 때 같은 순위의 책이 그대로 나와요. 다시 로그인하면 초기화돼요.")

    if st.button("추천 받기", type="primary", use_container_width=True):
        status = st.status("추천 준비 중...", expanded=False)
        rec = get_rec(progress=lambda m: status.update(label=m),
                      config={"W_CONTENT": w_content, "MAX_PER_AUTHOR": max_author},
                      exclude=list(st.session_state.shown.values()) if fresh else None)
        try:
            if genres:
                results, warns = rec.recommend_genre(genres, balanced=balanced)
            else:
                results, warns = rec.recommend_all()
            st.session_state.results = results
            st.session_state.warns = warns
            st.session_state.hidden = set()
            for b in results:
                st.session_state.shown[b["isbn13"]] = {"isbn13": b["isbn13"], "title": b.get("title")}
            if fresh and not results:
                st.session_state.warns = warns + ["새로 보여줄 책이 없어요. 위 스위치를 끄면 이전 추천을 다시 볼 수 있어요."]
            status.update(label="추천 완료!", state="complete")
        except Exception as e:
            status.update(label="오류가 났어요", state="error")
            st.error(f"추천 중 오류: {e}")

    for w in st.session_state.warns:
        st.warning(w)

    for b in st.session_state.results:
        isbn = b["isbn13"]
        if isbn in st.session_state.hidden:
            continue
        with st.container(border=True):
            c1, c2 = st.columns([1, 4])
            thumb(c1, b)
            book_header(c2, b)
            labels = []
            if b.get("tag"):
                labels.append(f"[{b['tag']}]")
            if b.get("is_new"):
                labels.append("✨ 처음 보는 작가")
            if b.get("score") is not None:
                labels.append(f"종합 {b['score']:.2f} (내용 {b['c']:.2f} · 대출 {b['k']:.2f})")
            if labels:
                c2.caption("  ".join(labels))
            if b.get("contents"):
                c2.write(b["contents"][:180] + ("…" if len(b["contents"]) > 180 else ""))

            a1, a2, a3 = st.columns(3)
            if a1.button("📌 읽고 싶어요", key=f"want_{isbn}", use_container_width=True):
                get_rec().save_feedback(b, "want")
                st.session_state.hidden.add(isbn)
                st.toast(f"읽고 싶은 책에 추가: {b['title']}")
                st.rerun()
            if a2.button("🙅 관심 없어요", key=f"no_{isbn}", use_container_width=True):
                get_rec().save_feedback(b, "not_interested")
                st.session_state.hidden.add(isbn)
                st.toast("다음 추천부터 빠져요")
                st.rerun()
            with a3.popover("✅ 읽었어요", use_container_width=True):
                r = st.select_slider("별점", options=RATINGS, format_func=fmt_rating, key=f"rr_{isbn}")
                if st.button("저장", key=f"rs_{isbn}"):
                    get_rec().save_read(b, r)
                    st.session_state.hidden.add(isbn)
                    st.toast(f"내 서재에 저장: {b['title']}")
                    st.rerun()

# ---------- 책 기록하기 ----------
with tab_add:
    q = st.text_input("읽은 책 제목이나 저자로 검색", placeholder="예: 아몬드 손원평")
    if q:
        found = search_books(q)
        if not found:
            st.info("검색 결과가 없어요. 다른 검색어로 해보세요.")
        for b in found:
            isbn = b["isbn13"]
            with st.container(border=True):
                c1, c2 = st.columns([1, 4])
                thumb(c1, b)
                book_header(c2, b)
                r = c2.select_slider("별점", options=RATINGS, format_func=fmt_rating, key=f"ar_{isbn}")
                if c2.button("읽었어요로 저장", key=f"as_{isbn}", type="primary"):
                    get_rec().save_read(b, r)
                    st.success(f"저장했어요: {b['title']} ({fmt_rating(r)})")

# ---------- 내 서재 ----------
with tab_lib:
    rows = get_rec().list_read()
    st.caption(f"총 {len(rows)}권")
    for b in rows:
        isbn = b["isbn13"]
        with st.container(border=True):
            c1, c2 = st.columns([1, 4])
            thumb(c1, b)
            book_header(c2, b)
            if b.get("read_date"):
                c2.caption(f"기록한 날: {b['read_date']}")
            cur = b.get("rating")
            r = c2.select_slider("별점", options=RATINGS, value=cur if cur in RATINGS else None,
                                 format_func=fmt_rating, key=f"lr_{isbn}")
            s1, s2 = c2.columns(2)
            if s1.button("별점 저장", key=f"ls_{isbn}", disabled=(r == cur)):
                get_rec().update_rating(isbn, r)
                st.toast("별점을 바꿨어요")
                st.rerun()
            if s2.button("기록 삭제", key=f"ld_{isbn}"):
                get_rec().delete_read(isbn)
                st.toast(f"삭제했어요: {b.get('title')}")
                st.rerun()

# ---------- 읽고 싶은 책 ----------
with tab_want:
    wants = get_rec().list_want()
    st.caption(f"총 {len(wants)}권")
    for b in wants:
        isbn = b["isbn13"]
        with st.container(border=True):
            c1, c2 = st.columns([1, 4])
            thumb(c1, b)
            book_header(c2, b)
            w1, w2 = c2.columns(2)
            with w1.popover("✅ 읽었어요", use_container_width=True):
                r = st.select_slider("별점", options=RATINGS, format_func=fmt_rating, key=f"wr_{isbn}")
                if st.button("저장", key=f"ws_{isbn}"):
                    get_rec().save_read(b, r)
                    st.toast(f"내 서재로 옮겼어요: {b.get('title')}")
                    st.rerun()
            if w2.button("목록에서 빼기", key=f"wd_{isbn}", use_container_width=True):
                get_rec().remove_feedback(isbn, "want")
                st.rerun()

# ---------- 관심 없는 책 ----------
with tab_no:
    nos = get_rec().list_feedback("not_interested")
    st.caption(f"총 {len(nos)}권 · 여기 있는 책(다른 판본 포함)은 추천에 나오지 않아요")
    for b in nos:
        isbn = b["isbn13"]
        with st.container(border=True):
            c1, c2 = st.columns([1, 4])
            thumb(c1, b)
            book_header(c2, b)
            if c2.button("되돌리기 (다시 추천 받기)", key=f"nd_{isbn}"):
                get_rec().remove_feedback(isbn, "not_interested")
                st.toast(f"다시 추천 후보가 돼요: {b.get('title')}")
                st.rerun()
