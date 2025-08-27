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
def setup_chrome_options():
    """Streamlit Cloud環境でのChrome設定"""
    options = Options()
    
    options.add_argument("--headless=new")  # <-- 追加（Cloudで安定する）
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--disable-web-security")
    options.add_argument("--ignore-certificate-errors")

    return options

    
def get_chrome_driver_path():
    # secrets.toml や Streamlit Cloud の Secrets に明示的に設定された chromedriver のパスを優先
    if "chromedriver_path" in st.secrets.get("selenium", {}):
        return st.secrets["selenium"]["chromedriver_path"]
    # デフォルトパス
    return "/mount/src/fukui_film_c2/ff_app/chromedriver"


def install_chrome_and_driver():
    try:
        import chromedriver_binary
        logger.info("chromedriver_binary successfully imported")
        return True
    except ImportError:
        # secrets に chromedriver_path があるか確認
        chrome_path = st.secrets.get("selenium", {}).get("chromedriver_path", "")
        if chrome_path and os.path.exists(chrome_path) and os.access(chrome_path, os.X_OK):
            logger.info(f"Using provided chromedriver at: {chrome_path}")
            return True
        else:
            logger.error(f"chromedriver path invalid or not executable: {chrome_path}")
            return False



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

        status = data.get("status", "")

        if status == "ZERO_RESULTS":
            logger.warning(f"Geocoding ZERO_RESULTS for {place_name}")
            return "", "", ""  # 空欄で返すことで登録は続行
        elif status != "OK":
            logger.warning(f"Geocoding failed for {place_name}: {status}")
            raise Exception(f"Geocoding failed: {status}")

        r = data["results"][0]
        return r["formatted_address"], r["geometry"]["location"]["lat"], r["geometry"]["location"]["lng"]
    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        return "", "", ""  # エラー時も空欄で返す





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

import os

# Streamlit Cloud 上で chromedriver に実行権限を強制付与
chromedriver_path = st.secrets["selenium"]["chromedriver_path"]
if os.path.exists(chromedriver_path):
    os.chmod(chromedriver_path, 0o755)







def run_fc_registration(user, pwd, headless, session_dir, metadata):
    logger.info("Starting FC registration process")
    
    # Chrome環境のセットアップ
    if not install_chrome_and_driver():
        raise Exception("Failed to setup Chrome environment")
    
    options = setup_chrome_options()
    if not headless:
        options.remove_argument("--headless")
    
    driver_path = get_chrome_driver_path()
    logger.info(f"Using ChromeDriver path: {driver_path}")
    
    driver = None
    try:
        # Chromeドライバーの起動
        # セットアップ済みの chromedriver を使う
        driver_path = st.secrets["selenium"]["chromedriver_path"]
        service = ChromeService(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=options)

        wait = WebDriverWait(driver, 40)
        
        logger.info("Chrome driver started successfully")
        
        # 1) ログイン
        logger.info("Step 1: Logging in to FC site")
        driver.get(f"{FC_BASE_URL}/login.php")
        
        login_id_element = wait.until(EC.visibility_of_element_located((By.NAME, "login_id")))
        login_id_element.send_keys(user)
        
        password_element = driver.find_element(By.NAME, "password")
        password_element.send_keys(pwd)
        
        login_button = driver.find_element(By.NAME, "login")
        login_button.click()
        
        logger.info("Login completed")
        
        # 2) 新規登録ページへ
        logger.info("Step 2: Navigating to registration page")
        driver.get(f"{FC_BASE_URL}/location/?mode=detail&id=0")
        wait.until(EC.presence_of_element_located((By.NAME, "name_ja")))
        
        # 2.1) 画像登録モーダルを開いて全画像アップロード
        logger.info("Step 2.1: Opening image upload modal")
        btn_add = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-toggle='modal'][data-target='#modal-img-add']")
        ))
        driver.execute_script("arguments[0].scrollIntoView(true);", btn_add)
        driver.execute_script("arguments[0].click();", btn_add)
        
        file_input = wait.until(EC.presence_of_element_located((By.ID, "InputFile")))
        
        # 圧縮済み画像をすべて選択
        paths = [
            os.path.abspath(os.path.join(session_dir, fn))
            for fn in os.listdir(session_dir)
            if fn.startswith("compressed_") and fn.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
        
        logger.info(f"Uploading {len(paths)} images")
        file_input.send_keys("\n".join(paths))
        
        # アップロードリスト数を待機
        expected_count = len(paths)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#files li.media")) >= expected_count)
        
        # 完了ステータスが揃うまで無制限ループ
        logger.info("Waiting for upload completion")
        while True:
            bars = driver.find_elements(By.CSS_SELECTOR, "#files li.media .progress-bar")
            statuses = driver.find_elements(By.CSS_SELECTOR, "#files li.media .status")
            if (len(bars) >= expected_count and len(statuses) >= expected_count
                and all(bar.get_attribute("aria-valuenow") == "100" for bar in bars)
                and all("Complete" in status.text for status in statuses)):
                break
            time.sleep(0.5)
        
        logger.info("Image upload completed")
        
        # モーダルを閉じる
        close_add = driver.find_element(By.CSS_SELECTOR, "#modal-img-add button[data-dismiss='modal']")
        driver.execute_script("arguments[0].click();", close_add)
        
        # 3) 地名／ふりがな／所在地 入力
        logger.info("Step 3: Filling location information")
        for field_name, value in [
            ("name_ja", metadata.get("place", "")),
            ("name_kana", metadata.get("furigana", "")),
            ("place_ja", metadata.get("address", ""))
        ]:
            el = driver.find_element(By.NAME, field_name)
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            el.clear()
            el.send_keys(value)
            logger.info(f"Filled {field_name}: {value}")
        
        # 4) 緯度経度取得
        logger.info("Step 4: Getting coordinates")
        btn_geo = driver.find_element(By.ID, "btn-g-search")
        driver.execute_script("arguments[0].scrollIntoView(true);", btn_geo)
        driver.execute_script("arguments[0].click();", btn_geo)
        wait.until(lambda d: d.find_element(By.NAME, "lat").get_attribute("value") != "")
        
        # 5) 概要
        logger.info("Step 5: Filling description")
        desc_el = driver.find_element(By.ID, "entry-description-ja")
        driver.execute_script("arguments[0].scrollIntoView(true);", desc_el)
        desc_el.clear()
        desc_el.send_keys(metadata.get("description", ""))
        
        # 6) 非公開フラグ
        logger.info("Step 6: Setting privacy flag")
        sel = driver.find_element(By.NAME, "activated")
        for opt in sel.find_elements(By.TAG_NAME, "option"):
            if opt.get_attribute("value") == "0":
                driver.execute_script("arguments[0].scrollIntoView(true);", opt)
                opt.click()
                break
        
        # 7) メイン画像選択
        main_file = metadata.get("main_file")
        if main_file:
            logger.info(f"Step 7: Setting main image: {main_file}")
            btn_main = wait.until(EC.element_to_be_clickable((By.ID, "select-main-img")))
            driver.execute_script("arguments[0].scrollIntoView(true);", btn_main)
            driver.execute_script("arguments[0].click();", btn_main)
            wait.until(EC.visibility_of_element_located((By.ID, "modal-img-select")))
            time.sleep(0.5)
            
            for box in driver.find_elements(By.CSS_SELECTOR, "#modal-img-select .select-img-box"):
                if main_file in box.text:
                    link = box.find_element(By.CSS_SELECTOR, "a.select-img-vw")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", link)
                    break
            time.sleep(8)
        
        # 8) サブ画像選択
        sub_files = metadata.get("sub_files") or []
        if sub_files:
            logger.info(f"Step 8: Setting sub images: {len(sub_files)} files")
            for fname in sub_files:
                logger.info(f"Processing sub image: {fname}")
                
                # 「画像選択」ボタンをクリックしてモーダル表示
                btn_sub = wait.until(EC.element_to_be_clickable((By.ID, "select-sub-img")))
                driver.execute_script("arguments[0].scrollIntoView(true);", btn_sub)
                btn_sub.click()
                time.sleep(5)
                
                # モーダルが開かれ、検索用入力欄が表示されるまで待機
                wait.until(EC.visibility_of_element_located((By.ID, "modal-img-select")))
                time.sleep(5)
                
                # 検索語を入力
                input_search = wait.until(EC.presence_of_element_located((By.ID, "search-file-name")))
                driver.execute_script("arguments[0].scrollIntoView(true);", input_search)
                input_search.clear()
                input_search.send_keys(fname)
                
                # 検索実行ボタンをクリック
                btn_search = driver.find_element(By.ID, "search-img")
                driver.execute_script("arguments[0].scrollIntoView(true);", btn_search)
                btn_search.click()
                
                # 検索結果が返ってくるのを待機
                wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#modal-img-select .select-img-box")))
                time.sleep(8)
                
                # 一件目の「選択」ボタンをクリック
                first_box = driver.find_elements(By.CSS_SELECTOR, "#modal-img-select .select-img-box")[0]
                link = first_box.find_element(By.CSS_SELECTOR, "a.select-img-vw")
                driver.execute_script("arguments[0].scrollIntoView(true);", link)
                link.click()
                
                # 検索語をクリアして、次の周辺画像の検索に備える
                input_search.clear()
                time.sleep(5)
        
        # 9) カテゴリ選択
        logger.info("Step 9: Setting category")
        btn_cat = wait.until(EC.element_to_be_clickable((By.ID, "select-category-btn")))
        time.sleep(3)
        driver.execute_script("arguments[0].scrollIntoView(true);", btn_cat)
        btn_cat.click()
        time.sleep(8)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "input.category-modal-select")))
        time.sleep(3)
        
        cbs = driver.find_elements(By.CSS_SELECTOR, "input.category-modal-select")
        target = next((cb for cb in cbs if cb.get_attribute("value") == "133"), None)
        if not target and cbs:
            target = cbs[0]
        if target:
            driver.execute_script("arguments[0].scrollIntoView(true);", target)
            driver.execute_script("arguments[0].click();", target)
        
        # 10) 保存
        logger.info("Step 10: Saving registration")
        save_btn = wait.until(EC.element_to_be_clickable((By.ID, "save-btn")))
        driver.execute_script("arguments[0].scrollIntoView(true);", save_btn)
        driver.execute_script("arguments[0].click();", save_btn)
        
        try:
            wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".alert-success")))
            logger.info("登録成功のアラートが表示されました")
        except TimeoutException:
            logger.warning("登録成功のアラートが表示されませんでしたが、続行します")
        
        logger.info("FC registration completed successfully")

        
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
