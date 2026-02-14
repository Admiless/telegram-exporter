import streamlit as st
import asyncio
import os
import json
import shutil
import tempfile
from datetime import datetime, date
from telethon import TelegramClient, functions, types, errors

# --- Конфигурация страницы ---
st.set_page_config(page_title="Telegram Exporter Pro", layout="wide")

# --- Стили ---
st.markdown("""
<style>
    .stApp { background-color: var(--bg-color); }
    .chat-header { font-size: 28px; font-weight: 700; margin-bottom: 20px; color: inherit; }
    .stButton>button[kind="primary"] {
        background-color: #3390ec !important;
        color: white !important;
        width: 100% !important;
        border-radius: 8px !important;
        padding: 15px !important;
        font-weight: 500 !important;
        box-shadow: 0 4px 12px rgba(51, 144, 236, 0.3);
    }
    .stDownloadButton>button {
        background-color: #00d26a !important;
        color: white !important;
        width: 100% !important;
        border-radius: 8px !important;
    }
    .stCheckbox { padding: 5px 10px; border-bottom: 1px solid rgba(128, 128, 128, 0.1); }
</style>
""", unsafe_allow_html=True)

st.title("Telegram Exporter Pro")

# --- Session State ---
for key, val in {
    'authenticated': False, 'auth_status': None, 'folders': {"Все": []}, 
    'all_chats': [], 'needs_2fa': False, 'api_id': "", 'api_hash': "", 'phone': "", 'phone_code_hash': None
}.items():
    if key not in st.session_state: st.session_state[key] = val

# --- ASYNC HELPERS ---
def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(coro)
    finally: loop.close()

async def get_client(api_id, api_hash):
    session_name = "session_user"
    client = TelegramClient(session_name, int(api_id), api_hash)
    if not client.is_connected(): await client.connect()
    return client

# --- SIDEBAR (Всегда виден, если авторизован) ---
with st.sidebar:
    st.header("Настройки")
    if st.session_state.authenticated:
        st.subheader("Параметры выгрузки")
        date_mode = st.radio("Период", ["Все сообщения", "Диапазон дат"])
        sd, ed = None, None
        if date_mode == "Диапазон дат":
            sd = st.date_input("С", value=date(2023, 1, 1))
            ed = st.date_input("По", value=datetime.today())
        
        st.divider()
        exp_fmt = st.selectbox("Формат", ["JSON", "TXT"])
        dl_media = st.checkbox("Загружать медиа")
        
        st.divider()
        if st.button("🚪 Выход / Сброс"):
            st.session_state.clear()
            st.rerun()
    else:
        st.info("Авторизуйтесь, чтобы увидеть настройки")

# --- UI AUTH ---
if not st.session_state.authenticated:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Вход")
        aid_in = st.text_input("API ID", value=st.session_state.api_id)
        ahash_in = st.text_input("API Hash", type="password", value=st.session_state.api_hash)
        phone_in = st.text_input("Телефон", value=st.session_state.phone)
        
        if st.button("🔗 Подключиться"):
            st.session_state.api_id, st.session_state.api_hash, st.session_state.phone = aid_in, ahash_in, phone_in
            async def auth():
                client = await get_client(aid_in, ahash_in)
                try:
                    if not await client.is_user_authorized():
                        res = await client.send_code_request(phone_in)
                        return 'code', res.phone_code_hash, None
                    return 'ok', None, None
                except Exception as e: return 'err', None, str(e)
                finally: await client.disconnect()
            
            auth_res, code_h, err = run_async(auth())
            if auth_res == 'ok': st.session_state.authenticated = True; st.rerun()
            elif auth_res == 'code': st.session_state.auth_status = 'code_req'; st.session_state.phone_code_hash = code_h
            else: st.error(err)

    if st.session_state.auth_status == 'code_req':
        with c2:
            st.subheader("Код")
            code_v = st.text_input("Код из ТГ")
            pwd_v = st.text_input("Облачный пароль", type="password") if st.session_state.needs_2fa else ""
            if st.button("Войти"):
                async def sign():
                    client = await get_client(st.session_state.api_id, st.session_state.api_hash)
                    try:
                        await client.sign_in(st.session_state.phone, code_v, phone_code_hash=st.session_state.phone_code_hash)
                        return "ok"
                    except errors.SessionPasswordNeededError:
                        st.session_state.needs_2fa = True
                        if pwd_v: await client.sign_in(password=pwd_v); return "ok"
                        return "2fa"
                    finally: await client.disconnect()
                if run_async(sign()) == "ok": st.session_state.authenticated = True; st.rerun()

# --- UI MAIN ---
else:
    st.markdown('<div class="chat-header">Чаты</div>', unsafe_allow_html=True)
    col_f1, col_f2 = st.columns([3, 1])
    with col_f1:
        selected_folder = st.selectbox("Выбор папки", list(st.session_state.folders.keys()))
    with col_f2:
        st.write("")
        if st.button("🔄 Обновить", use_container_width=True):
            async def get_chats():
                client = await get_client(st.session_state.api_id, st.session_state.api_hash)
                try:
                    dialogs = await client.get_dialogs()
                    chats = []
                    for d in dialogs:
                        t = "user"
                        if d.is_channel: t = "channel"
                        elif d.is_group: t = "group"
                        if getattr(d.entity, 'bot', False): t = "bot"
                        chats.append({'id': d.id, 'title': d.title, 'username': getattr(d.entity, 'username', ''), 'type': t})
                    
                    # 1. Системные папки
                    folders = {"Все": [c['id'] for c in chats]}

                    # 2. Ваши личные папки из Telegram
                    try:
                        from telethon import utils
                        d_filters = await client(functions.messages.GetDialogFiltersRequest())
                        for f in d_filters:
                            if hasattr(f, 'title') and f.title:
                                folder_ids = []
                                if hasattr(f, 'include_peers'):
                                    for p in f.include_peers:
                                        try:
                                            # Получаем правильный ID (с учетом типа чата -100...)
                                            peer_id = utils.get_peer_id(p)
                                            folder_ids.append(peer_id)
                                        except: pass
                                
                                # Добавляем даже пустые папки (они могут быть по типам)
                                folders[f.title] = folder_ids
                        
                        st.toast(f"Найдено папок в аккаунте: {len(d_filters)}")
                    except Exception as e:
                        pass
                    
                    folders["🤖 Боты"] = [c['id'] for c in chats if c['type'] == "bot"]
                    folders["📢 Каналы"] = [c['id'] for c in chats if c['type'] == "channel"]
                    folders["👤 Личные"] = [c['id'] for c in chats if c['type'] == "user"]
                    return folders, chats
                finally: await client.disconnect()
            
            with st.spinner("Загрузка папок..."):
                st.session_state.folders, st.session_state.all_chats = run_async(get_chats())
                st.rerun()

    search_q = st.text_input("", placeholder="Поиск чатов...", label_visibility="collapsed")
    
    show = st.session_state.all_chats
    if selected_folder != "Все":
        fids = st.session_state.folders.get(selected_folder, [])
        show = [c for c in show if c['id'] in fids]
    if search_q: show = [c for c in show if search_q.lower() in c['title'].lower()]

    st.caption(f"Найдено: {len(show)}")
    sel_chats = [c for c in show[:100] if st.checkbox(f"{c['title']} (@{c['username']})", key=f"bx_{c['id']}")]

    if st.button("Экспортировать выбранное", type="primary"):
        if sel_chats:
            temp_dir = tempfile.mkdtemp()
            exp_path = os.path.join(temp_dir, f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            os.makedirs(exp_path, exist_ok=True)

            async def do_exp():
                client = await get_client(st.session_state.api_id, st.session_state.api_hash)
                try:
                    for t in sel_chats:
                        chat_dir = os.path.join(exp_path, str(t['id']))
                        os.makedirs(chat_dir, exist_ok=True)
                        ms = []
                        off = datetime.combine(ed, datetime.max.time()) if 'ed' in locals() and ed else None
                        async for m in client.iter_messages(t['id'], limit=1000, offset_date=off):
                            if 'sd' in locals() and sd and m.date.date() < sd: break
                            ms.append({'d': m.date.isoformat(), 't': m.text or ""})
                        
                        f_name = "data.json" if exp_fmt == "JSON" else "data.txt"
                        p = os.path.join(chat_dir, f_name)
                        with open(p, 'w', encoding='utf-8') as f:
                            if exp_fmt == "JSON": json.dump(ms, f, ensure_ascii=False, indent=2)
                            else: [f.write(f"[{x['d']}] {x['t']}\n---\n") for x in ms]
                    
                    zip_p = os.path.join(temp_dir, "telegram_export")
                    shutil.make_archive(zip_p, 'zip', exp_path)
                    return zip_p + ".zip"
                finally: await client.disconnect()

            with st.spinner("Создаем архив..."):
                z = run_async(do_exp())
                with open(z, "rb") as f:
                    st.download_button("⬇️ СКАЧАТЬ АРХИВ (ZIP)", f, file_name=os.path.basename(z), mime="application/zip")
                st.success("Готов к скачиванию!")
        else: st.warning("Выберите чаты.")
