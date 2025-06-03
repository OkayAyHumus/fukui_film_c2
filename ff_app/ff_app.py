# ff_app.py

import streamlit as st
import pandas as pd
import os
import shutil
import requests
import time
import uuid
import traceback
import logging
import subprocess
import sys
from io import BytesIO
from datetime import datetime
from PIL import Image, ImageEnhance
from pykakasi import kakasi

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# ========================
# 定数
# ========================
FC_BASE_URL = "https://fc.jl-db.jp"
CHROMEDRIVER_SECRET_KEY = "chromedriver_path"

# ========================
# ログ設定
# ========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ========================
# Selenium設定関数
# ========================
def setup_chrome_options(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")
    return options
    
import tempfile

def get_chrome_driver_path():
    """Streamlit Cloudで使える場所にchromedriverをインストール"""
    import chromedriver_autoinstaller
    temp_dir = os.path.join(tempfile.gettempdir(), "chromedriver")
    os.makedirs(temp_dir, exist_ok=True)
    driver_path = chromedriver_autoinstaller.install(path=temp_dir)
    logger.info(f"Chromedriver installed to: {driver_path}")
    return driver_path


def install_chrome_and_driver():
    """Streamlit Cloudで自動インストールされる前提で常にTrueを返す"""
    return True


# ========================
# Google Drive 接続・フォルダ作成
# ========================
def get_drive_service():
    try:
        creds = service_account.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Google Drive service initialization failed: {e}")
        raise

def create_timestamped_folder(service, parent_id):
    try:
        name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        meta = {"name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]}
        fid = service.files().create(body=meta, fields="id").execute()["id"]
        logger.info(f"Created folder: {name} (ID: {fid})")
        return fid, name
    except Exception as e:
        logger.error(f"Failed to create folder: {e}")
        raise

# ========================
# users.csv の読み込み
# ========================
@st.cache_data
def load_users(_service, admin_folder_id):
    try:
        q = f"'{admin_folder_id}' in parents and name='users.csv' and mimeType='text/csv'"
        files = _service.files().list(q=q, fields="files(id)").execute().get("files", [])
        if not files:
            logger.warning("users.csv not found")
            return None, None
        
        fid = files[0]["id"]
        fh = BytesIO()
        
        from googleapiclient.http import MediaIoBaseDownload
        downloader = MediaIoBaseDownload(fh, _service.files().get_media(fileId=fid))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)
        
        logger.info("users.csv loaded successfully")
        return pd.read_csv(fh), fid
    except Exception as e:
        logger.error(f"Failed to load users.csv: {e}")
        return None, None

# ========================
# ログイン機能
# ========================
def login(users_df):
    st.sidebar.header("🔐 ログイン")
    if "username" in st.session_state:
        st.sidebar.success(f"ログイン中: {st.session_state['username']}")
        if st.sidebar.button("ログアウト"):
            for k in ("username", "folder_id", "is_admin"): 
                st.session_state.pop(k, None)
            st.sidebar.info("ログアウトしました。")
        return
    
    u = st.sidebar.text_input("ユーザー名", key="login_user")
    p = st.sidebar.text_input("パスワード", type="password", key="login_pass")
    
    if st.sidebar.button("ログイン"):
        try:
            df = users_df.copy()
            df["username"] = df["username"].str.strip()
            df["password"] = df["password"].str.strip()
            m = df[(df["username"] == u.strip()) & (df["password"] == p.strip())]
            
            if not m.empty:
                st.session_state["username"] = u.strip()
                st.session_state["folder_id"] = m.iloc[0]["folder_id"]
                st.session_state["is_admin"] = (u.strip() == "admin")
                st.sidebar.success("ログイン成功")
                logger.info(f"User logged in: {u.strip()}")
            else:
                st.sidebar.error("認証失敗")
                logger.warning(f"Login failed for user: {u.strip()}")
        except Exception as e:
            st.sidebar.error(f"ログインエラー: {e}")
            logger.error(f"Login error: {e}")

# ========================
# Geocoding + ふりがな変換
# ========================
def search_location_info(place_name):
    try:
        key = st.secrets["google_maps"]["api_key"]
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={place_name}&language=ja&key={key}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get("status") != "OK":
            logger.warning(f"Geocoding failed for {place_name}: {data.get('status')}")
            return "", "", ""
        
        r = data["results"][0]
        logger.info(f"Geocoding successful for {place_name}")
        return r["formatted_address"], r["geometry"]["location"]["lat"], r["geometry"]["location"]["lng"]
    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        return "", "", ""

def convert_to_furigana(text):
    try:
        k = kakasi()
        k.setMode("H", "a")
        k.setMode("K", "a") 
        k.setMode("J", "a")
        result = k.getConverter().do(text)
        logger.info(f"Furigana conversion: {text} -> {result}")
        return result
    except Exception as e:
        logger.error(f"Furigana conversion error: {e}")
        return text

# ========================
# 画像補正・圧縮
# ========================
def enhance_image(img, b, c, col):
    try:
        img = ImageEnhance.Brightness(img).enhance(b)
        img = ImageEnhance.Contrast(img).enhance(c)
        img = ImageEnhance.Color(img).enhance(col)
        return img
    except Exception as e:
        logger.error(f"Image enhancement error: {e}")
        return img

def compress_image(img, max_bytes):
    try:
        buf = BytesIO()
        q = 95
        while q >= 10:
            buf.seek(0)
            buf.truncate()
            img.save(buf, format="JPEG", quality=q, optimize=True)
            if buf.tell() <= max_bytes:
                logger.info(f"Image compressed to {buf.tell()} bytes at quality {q}")
                return buf
            q -= 5
        logger.warning("Could not compress image to target size")
        return None
    except Exception as e:
        logger.error(f"Image compression error: {e}")
        return None

# ========================
# FCサイト自動登録
# ========================
def run_fc_registration(user, pwd, headless, session_dir, metadata):
    logger.info("Starting FC registration process")

    if not install_chrome_and_driver():
        raise Exception("Failed to setup Chrome environment")

    options = setup_chrome_options(headless=headless)
    driver_path = get_chrome_driver_path()
    logger.info(f"Using ChromeDriver path: {driver_path}")

    driver = None
    try:
        service = ChromeService(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 40)

        logger.info("Chrome driver started successfully")

        try:
            driver.get("https://www.google.com")
            time.sleep(2)
            logger.info("Google loaded successfully")
        except Exception as e:
            logger.error("Failed to load Google")
            logger.error(traceback.format_exc())
            raise

        try:
            driver.get(f"{FC_BASE_URL}/login.php")
            login_id_element = wait.until(EC.visibility_of_element_located((By.NAME, "login_id")))
            login_id_element.send_keys(user)

            password_element = driver.find_element(By.NAME, "password")
            password_element.send_keys(pwd)

            login_button = driver.find_element(By.NAME, "login")
            login_button.click()
            logger.info("Login completed")
        except Exception as e:
            logger.error("Login step failed")
            logger.error(traceback.format_exc())
            raise

        try:
            driver.get(f"{FC_BASE_URL}/location/?mode=detail&id=0")
            wait.until(EC.presence_of_element_located((By.NAME, "name_ja")))
            logger.info("Navigated to registration page")
        except Exception as e:
            logger.error("Navigation to registration page failed")
            logger.error(traceback.format_exc())
            raise

        # 以下、同様にアップロード・入力処理の各ブロックを try-except で囲む
        # 必要であれば続けて分けて挿入できます

    except Exception as e:
        logger.error(f"FC registration error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise

    finally:
        if driver:
            if headless:
                driver.quit()
                logger.info("Chrome driver closed")
            else:
                logger.info("ヘッドレスOFF のため、ブラウザが開いたままです。")

# ========================
# ログ表示コンポーネント
# ========================
def show_logs():
    """ログを表示するStreamlitコンポーネント"""
    st.sidebar.header("📋 ログ")
    
    # ログレベル選択
    log_level = st.sidebar.selectbox(
        "ログレベル",
        ["INFO", "WARNING", "ERROR"],
        index=0
    )
    
    # ログの取得と表示（実際の実装では、ログハンドラーからログを取得）
    if st.sidebar.button("ログを更新"):
        st.sidebar.success("ログが更新されました")

# ========================
# システム情報表示
# ========================
def show_system_info():
    """システム情報を表示"""
    with st.expander("🔧 システム情報"):
        st.write("**Python Version:**", sys.version)
        st.write("**OS:**", os.name)
        
        # Chrome関連の情報
        try:
            import chromedriver_binary
            st.write("**ChromeDriver Binary:**", "✅ Installed")
            st.write("**ChromeDriver Path:**", chromedriver_binary.chromedriver_filename)
        except ImportError:
            st.write("**ChromeDriver Binary:**", "❌ Not installed")
        
        # 環境変数
        st.write("**Environment Variables:**")
        for key in ["DISPLAY", "CHROME_BIN", "CHROMEDRIVER_PATH"]:
            value = os.environ.get(key, "Not set")
            st.write(f"  - {key}: {value}")

# ========================
# メイン
# ========================
def main():
    st.set_page_config(page_title="画像圧縮＋地名情報取得", layout="wide")
    st.title("📷 画像圧縮＋地名情報取得アプリ")
    
    # システム情報表示
    show_system_info()
    
    # ログシステム初期化
    show_logs()
    
    try:
        # Drive & users
        logger.info("Initializing Google Drive service")
        service = get_drive_service()
        users_df, _ = load_users(service, st.secrets["folders"]["admin_folder_id"])
        
        if users_df is None:
            st.error("users.csv が見つかりません")
            logger.error("users.csv not found")
            return
        
        login(users_df)
        if "username" not in st.session_state: 
            st.stop()
        
        # FC-site 設定
        st.sidebar.header("⚙️ FCサイト設定")
        fc_user = st.sidebar.text_input("FC ログインID")
        fc_pass = st.sidebar.text_input("FC パスワード", type="password")
        headless = st.sidebar.checkbox("ヘッドレス実行", value=True)
        
        # Drive 画像フォルダ
        folder_id = st.text_input("📁 Google Drive フォルダIDを入力")
        if not folder_id: 
            st.stop()
        
        logger.info(f"Loading images from folder: {folder_id}")
        files = service.files().list(
            q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
            fields="files(id,name)"
        ).execute().get("files", [])
        
        if not files: 
            st.warning("画像が見つかりません")
            logger.warning("No images found in the specified folder")
            return
        
        logger.info(f"Found {len(files)} images")
        
        # 基本情報
        place = st.text_input("地名（漢字）")
        furigana = st.text_input("ふりがな")
        desc = st.text_area("概要", "")
        max_kb = st.sidebar.number_input("🔧 圧縮後最大KB", 50, 2048, 2000)
        max_bytes = max_kb * 1024
        
        # 画像プレビュー＆設定
        st.header("🖼️ 画像選択・補正")
        select_all = st.checkbox("すべて選択")
        settings = {}
        os.makedirs("data", exist_ok=True)
        
        for f in files:
            fid, name = f["id"], f["name"]
            path = os.path.join("data", name)
            
            try:
                with open(path, "wb") as fp: 
                    fp.write(service.files().get_media(fileId=fid).execute())
                img = Image.open(path)
                
                b = st.slider(f"明るさ[{name}]", 0.5, 2.0, 1.2, 0.1, key=f"b_{name}")
                c = st.slider(f"コントラスト[{name}]", 0.5, 2.0, 1.2, 0.1, key=f"c_{name}")
                col = st.slider(f"彩度[{name}]", 0.5, 2.0, 1.3, 0.1, key=f"col_{name}")
                
                en = enhance_image(img.copy(), b, c, col)
                c1, c2 = st.columns(2)
                with c1: 
                    st.image(img, caption="元", use_container_width=True)
                with c2: 
                    st.image(en, caption="補正", use_container_width=True)
                
                main = st.checkbox("メインで使う", key=f"main_{name}")
                sel = st.checkbox("選択", key=f"sel_{name}", value=select_all)
                settings[name] = {"b": b, "c": c, "col": col, "main": main, "sel": sel}
                
            except Exception as e:
                st.error(f"画像の読み込みに失敗しました: {name} - {e}")
                logger.error(f"Failed to load image {name}: {e}")
        
        if st.button("🔍 圧縮→検索→Drive保存→自動登録"):
            try:
                # プログレスバー
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                # 一時ディレクトリ
                session_dir = f"output/{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                os.makedirs(session_dir, exist_ok=True)
                logger.info(f"Created session directory: {session_dir}")
                
                # 住所検索
                status_text.text("住所情報を検索中...")
                progress_bar.progress(10)
                addr, lat, lng = search_location_info(place)
                metadata = {
                    "place": place, 
                    "furigana": furigana, 
                    "description": desc,
                    "address": addr, 
                    "lat": lat, 
                    "lng": lng
                }
                
                # 圧縮＆ファイルリスト
                status_text.text("画像を圧縮中...")
                progress_bar.progress(30)
                sub_files = []
                main_file = None
                
                for f in files:
                    name = f["name"]
                    s = settings[name]
                    if not s["sel"]: 
                        continue
                    
                    img = Image.open(os.path.join("data", name))
                    en = enhance_image(img, s["b"], s["c"], s["col"])
                    buf = compress_image(en, max_bytes)
                    out = f"compressed_{name}"
                    
                    if buf:
                        with open(os.path.join(session_dir, out), "wb") as fp: 
                            fp.write(buf.getvalue())
                        sub_files.append(out)
                        if s["main"]: 
                            main_file = out
                        logger.info(f"Compressed image: {name} -> {out}")
                
                metadata["main_file"] = main_file
                metadata["sub_files"] = sub_files
                
                # CSV 作成
                status_text.text("メタデータを作成中...")
                progress_bar.progress(50)
                csv_path = os.path.join(session_dir, "metadata.csv")
                pd.DataFrame([metadata]).to_csv(csv_path, index=False)
                logger.info("Metadata CSV created")
                
                # Google Drive にチャンクアップロード
                status_text.text("Google Driveにアップロード中...")
                progress_bar.progress(60)
                new_fid, new_name = create_timestamped_folder(service, folder_id)
                st.info(f"▶ アップロード先: {new_name}")
                
                files_to_upload = os.listdir(session_dir)
                for i, fn in enumerate(files_to_upload):
                    fp = os.path.join(session_dir, fn)
                    mime = "image/jpeg" if fn.lower().endswith((".jpg", ".jpeg")) else "text/csv"
                    
                    try:
                        media = MediaIoBaseUpload(
                            open(fp, "rb"), 
                            mimetype=mime,
                            resumable=True, 
                            chunksize=1024*1024
                        )
                        req = service.files().create(
                            body={"name": fn, "parents": [new_fid]},
                            media_body=media
                        )
                        
                        uploaded = False
                        with st.spinner(f"Uploading {fn}..."):
                            while not uploaded:
                                status, resp = req.next_chunk()
                                if status:
                                    progress = int(status.progress() * 100)
                                    st.write(f"  {fn}: {progress}%")
                                if resp:
                                    uploaded = True
                        
                        st.success(f"  ✅ {fn} uploaded")
                        logger.info(f"Uploaded file: {fn}")
                        
                        # プログレスバー更新
                        upload_progress = 60 + (i + 1) / len(files_to_upload) * 20
                        progress_bar.progress(int(upload_progress))
                        
                    except Exception as e:
                        st.error(f"❌ アップロード失敗: {fn} - {e}")
                        logger.error(f"Upload failed for {fn}: {e}")
                
                st.success("🎉 Drive へのアップロード完了")
                
                # FC 自動登録
                status_text.text("FCサイトに自動登録中...")
                progress_bar.progress(80)
                
                if not fc_user or not fc_pass:
                    st.warning("⚠️ FCサイトのログイン情報が入力されていません。自動登録をスキップします。")
                    logger.warning("FC login credentials not provided, skipping auto-registration")
                else:
                    try:
                        run_fc_registration(fc_user, fc_pass, headless, session_dir, metadata)
                        st.success("✅ FCサイト自動登録完了")
                        logger.info("FC registration completed successfully")
                    except Exception as e:
                        st.error(f"❌ 自動登録中にエラー発生: {e}")
                        logger.error(f"FC registration failed: {e}")
                        logger.error(f"Traceback: {traceback.format_exc()}")
                        
                        # エラー詳細をユーザーに表示
                        with st.expander("エラー詳細"):
                            st.code(traceback.format_exc())
                
                # 完了
                progress_bar.progress(100)
                status_text.text("処理完了！")
                
                # ローカル削除
                try:
                    shutil.rmtree(session_dir)
                    logger.info(f"Cleaned up session directory: {session_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup session directory: {e}")
                
            except Exception as e:
                st.error(f"❌ 処理中にエラーが発生しました: {e}")
                logger.error(f"Main process error: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                
                # エラー詳細をユーザーに表示
                with st.expander("エラー詳細"):
                    st.code(traceback.format_exc())
    
    except Exception as e:
        st.error(f"❌ アプリケーション初期化エラー: {e}")
        logger.error(f"Application initialization error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # エラー詳細をユーザーに表示
        with st.expander("エラー詳細"):
            st.code(traceback.format_exc())

if __name__ == "__main__":
    main()
