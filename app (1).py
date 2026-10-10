import hashlib
import hmac
import re
import secrets as pysecrets
import json

import streamlit as st
from streamlit_js_eval import streamlit_js_eval
from supabase import create_client

from recommender import GENRES, Recommender

st.set_page_config(page_title="다음책", page_icon="📚", layout="centered")


def secret(name, default=None):
    try:
        return st.secrets[name]
    except Exception:
        return default


@st.cache_resource
def get_sb():
    return create_client(secret("SUPABASE_URL"), secret("SUPABASE_KEY"))


# ---------- 계정 ----------
# 1) Secrets의 [USERS] (관리자 계정, 기존 계정)
# 2) Supabase app_users 테이블 (사이트의 '회원 관리'에서 등록한 계정, 비밀번호는 암호화해서 저장)
try:
    SECRET_USERS = {str(k): str(v) for k, v in dict(secret("USERS") or {}).items()}
except Exception:
    SECRET_USERS = {}
ADMINS = set(str(secret("ADMINS", "hyeyeon")).replace(" ", "").split(","))
ID_RE = re.compile(r"^[a-z0-9_]{2,20}$")


def hash_pw(pw, salt=None):
    salt = salt or pysecrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def db_users():
    """사이트에서 등록한 계정 목록. 테이블이 아직 없으면 None"""
    try:
        res = get_sb().table("app_users").select("user_id, pw_hash, created_at").order("created_at").execute()
        return {r["user_id"]: r for r in res.data}
    except Exception:
        return None


def login_ok(uid, pw):
    if uid in SECRET_USERS:
        return hmac.compare_digest(SECRET_USERS[uid], pw)
    row = (db_users() or {}).get(uid)
    return bool(row) and check_pw(pw, row["pw_hash"])


def set_password(uid, pw):
    get_sb().table("app_users").upsert({"user_id": uid, "pw_hash": hash_pw(pw)}).execute()


# ---------- 로그인 (아이디 저장) ----------
# '아이디 저장'을 켜면 이 브라우저(localStorage)에 아이디만 저장해서 다음에 자동으로 채워줌.
# 비밀번호는 저장하지 않음.
SAVED_ID_KEY = "nextbook_saved_id"

if not st.session_state.get("user_id"):
    st.title("📚 다음책")
    saved = streamlit_js_eval(js_expressions=f"localStorage.getItem('{SAVED_ID_KEY}') || ''",
                              key="read_saved_id")
    saved = saved if isinstance(saved, str) and ID_RE.match(saved) else ""
    with st.form("login"):
        uid = st.text_input("아이디", value=saved, key=f"uid_{saved}", autocomplete="username").strip().lower()
        pw = st.text_input("비밀번호", type="password", autocomplete="current-password")
        remember = st.checkbox("아이디 저장", value=bool(saved), key=f"remember_{saved}")
        ok = st.form_submit_button("들어가기", type="primary", use_container_width=True)
    if ok:
        if uid and pw and login_ok(uid, pw):
            st.session_state.user_id = uid
            st.session_state.save_id = uid if remember else ""
            st.rerun()
        else:
            st.error("아이디나 비밀번호가 달라요.")
    st.stop()

# 로그인 직후 한 번만: 아이디를 브라우저에 저장하거나 지움
if "save_id" in st.session_state:
    sid = st.session_state.pop("save_id")
    js = (f"localStorage.setItem('{SAVED_ID_KEY}', {json.dumps(sid)})" if sid
          else f"localStorage.removeItem('{SAVED_ID_KEY}')")
    streamlit_js_eval(js_expressions=js, key=f"write_saved_id_{sid}")

USER_ID = st.session_state.user_id
IS_ADMIN = USER_ID in ADMINS


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
tab_names = ["추천받기", "책 기록하기", "내 서재", "읽고 싶은 책", "관심 없는 책", "내 계정"]
if IS_ADMIN:
    tab_names.append("회원 관리")
tabs = st.tabs(tab_names)
tab_rec, tab_add, tab_lib, tab_want, tab_no, tab_me = tabs[:6]

# ---------- 추천받기 ----------
with tab_rec:
    genres = st.multiselect("장르 (비워두면 전체)", list(GENRES), placeholder="장르를 골라주세요")
    balanced = True
    if len(genres) >= 2:
        balanced = st.radio("방식", ["골고루", "내 취향 우선"], horizontal=True) == "골고루"
    with st.expander("세부 설정"):
        w_content = st.slider("책 소개 비중 (나머지는 대출 데이터)", 0.0, 1.0, 0.5, 0.1)
        max_author = st.slider("한 작가당 최대 권수", 1, 5, 2)
        include_kids = st.toggle("아동 도서(동화 등)도 포함", value=False)
    fresh = st.toggle("이번에 이미 본 책은 빼고 새 책으로", value=True,
                      help="끄면 다시 눌렀을 때 같은 순위의 책이 그대로 나와요. 다시 로그인하면 초기화돼요.")

    if st.button("추천 받기", type="primary", use_container_width=True):
        status = st.status("추천 준비 중...", expanded=False)
        rec = get_rec(progress=lambda m: status.update(label=m),
                      config={"W_CONTENT": w_content, "MAX_PER_AUTHOR": max_author,
                              "INCLUDE_KIDS": include_kids},
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

# ---------- 내 계정 (비밀번호 바꾸기) ----------
with tab_me:
    st.subheader("비밀번호 바꾸기")
    if USER_ID in SECRET_USERS:
        st.info("이 계정은 Streamlit 설정(Secrets)에 등록된 계정이라, 비밀번호도 거기서 바꿔야 해요.")
    elif db_users() is None:
        st.info("아직 계정 저장 공간이 준비되지 않았어요. 관리자에게 알려주세요.")
    else:
        with st.form("change_pw", clear_on_submit=True):
            cur = st.text_input("지금 비밀번호", type="password")
            new1 = st.text_input("새 비밀번호 (4자 이상)", type="password")
            new2 = st.text_input("새 비밀번호 한 번 더", type="password")
            go = st.form_submit_button("바꾸기", type="primary")
        if go:
            if not login_ok(USER_ID, cur):
                st.error("지금 비밀번호가 달라요.")
            elif len(new1) < 4:
                st.error("새 비밀번호는 4자 이상이어야 해요.")
            elif new1 != new2:
                st.error("새 비밀번호 두 개가 서로 달라요.")
            else:
                set_password(USER_ID, new1)
                st.success("비밀번호를 바꿨어요. 다음 로그인부터 새 비밀번호를 쓰세요.")

# ---------- 회원 관리 (관리자만) ----------
if IS_ADMIN:
    with tabs[6]:
        users = db_users()
        if users is None:
            st.error("회원 저장용 테이블(app_users)이 아직 없어요. Supabase SQL Editor에서 한 번만 만들어 주세요.")
            st.stop()

        st.subheader("친구 추가")
        with st.form("add_user", clear_on_submit=True):
            new_id = st.text_input("아이디 (영어 소문자·숫자·_ , 2~20자)").strip().lower()
            new_pw = st.text_input("비밀번호 (4자 이상)")
            add = st.form_submit_button("추가하기", type="primary", use_container_width=True)
        if add:
            if not ID_RE.match(new_id):
                st.error("아이디는 영어 소문자, 숫자, _ 만 쓸 수 있어요 (2~20자).")
            elif new_id in users or new_id in SECRET_USERS:
                st.error("이미 있는 아이디예요.")
            elif len(new_pw) < 4:
                st.error("비밀번호는 4자 이상이어야 해요.")
            else:
                set_password(new_id, new_pw)
                st.success(f"추가했어요! 친구에게 아이디 **{new_id}** / 비밀번호 **{new_pw}** 를 알려주세요.")
                users = db_users() or {}

        st.subheader(f"사이트에서 등록한 회원 ({len(users)}명)")
        if SECRET_USERS:
            st.caption("Streamlit 설정에 있는 계정: " + ", ".join(SECRET_USERS) + " (여기서는 수정 불가)")
        for uid, row in users.items():
            with st.container(border=True):
                st.markdown(f"**{uid}**")
                c1, c2 = st.columns([3, 1])
                pw_new = c1.text_input("새 비밀번호", key=f"rp_{uid}", label_visibility="collapsed",
                                       placeholder="새 비밀번호 (재설정)")
                if c2.button("재설정", key=f"rb_{uid}", use_container_width=True):
                    if len(pw_new) < 4:
                        st.error("비밀번호는 4자 이상이어야 해요.")
                    else:
                        set_password(uid, pw_new)
                        st.success(f"{uid} 비밀번호를 바꿨어요.")
                if uid != USER_ID:
                    with st.popover("로그인 막기(삭제)"):
                        st.write("이 아이디로 더 이상 로그인할 수 없게 돼요. 독서 기록은 남아 있어서, 같은 아이디로 다시 추가하면 복구돼요.")
                        if st.button("삭제", key=f"del_{uid}", type="primary"):
                            get_sb().table("app_users").delete().eq("user_id", uid).execute()
                            st.rerun()
