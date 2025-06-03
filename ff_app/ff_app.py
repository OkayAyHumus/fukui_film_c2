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
        wait = WebDriverWait(driver, 60)

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

        try:
            logger.info(f"住所検索対象: {metadata.get('address')}")
            btn_geo = wait.until(EC.element_to_be_clickable((By.ID, "btn-g-search")))
            driver.execute_script("arguments[0].scrollIntoView(true);", btn_geo)
            time.sleep(1)
            btn_geo.click()
            time.sleep(2)

            # Alertの有無を先にチェック
            try:
                alert = driver.switch_to.alert
                alert_text = alert.text
                logger.warning(f"Unexpected alert detected: {alert_text}")
                alert.accept()
                raise Exception(f"ジオコーディング失敗: {alert_text}")
            except NoAlertPresentException:
                logger.info("No alert detected after geocode click")

            # 緯度取得の確認（正常時のみ）
            wait.until(lambda d: d.find_element(By.NAME, "lat").get_attribute("value") != "")
            logger.info("Latitude successfully populated")

        except Exception as e:
            logger.error("Failed to get coordinates")
            logger.error(traceback.format_exc())
            raise

        # 以下、他の操作（アップロード、入力など）も必要に応じて try-except でラップ可能

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

