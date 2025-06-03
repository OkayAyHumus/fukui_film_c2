
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
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    return options

def get_chrome_driver_path():
    try:
        import chromedriver_autoinstaller
        import tempfile
        temp_dir = os.path.join(tempfile.gettempdir(), "chromedriver")
        os.makedirs(temp_dir, exist_ok=True)
        return chromedriver_autoinstaller.install(path=temp_dir)
    except ImportError:
        return "chromedriver"

def install_chrome_and_driver():
    try:
        import chromedriver_autoinstaller
        return True
    except ImportError:
        logger.warning("chromedriver_autoinstaller not found.")
        return False

# ========================
# Google Drive 接続
# ========================
def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"]
    )
    return build("drive", "v3", credentials=creds)

def create_timestamped_folder(service, parent_id):
    name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    file = service.files().create(body=file_metadata, fields="id").execute()
    return file.get("id"), name

# ========================
# ふりがな取得 & 住所取得
# ========================
def convert_to_furigana(text):
    kakasi_inst = kakasi()
    kakasi_inst.setMode("H", "a")
    kakasi_inst.setMode("K", "a")
    kakasi_inst.setMode("J", "a")
    conv = kakasi_inst.getConverter()
    return conv.do(text)

def search_location_info(place_name):
    key = st.secrets["google_maps"]["api_key"]
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={place_name}&language=ja&key={key}"
    r = requests.get(url)
    res = r.json()
    if res["status"] != "OK":
        return "", "", ""
    result = res["results"][0]
    return result["formatted_address"], result["geometry"]["location"]["lat"], result["geometry"]["location"]["lng"]

# ========================
# 画像補正 & 圧縮
# ========================
def enhance_image(img, b, c, col):
    img = ImageEnhance.Brightness(img).enhance(b)
    img = ImageEnhance.Contrast(img).enhance(c)
    img = ImageEnhance.Color(img).enhance(col)
    return img

def compress_image(img, max_bytes):
    buffer = BytesIO()
    quality = 95
    while quality > 10:
        buffer.seek(0)
        buffer.truncate()
        img.save(buffer, format="JPEG", quality=quality)
        if buffer.tell() <= max_bytes:
            return buffer
        quality -= 5
    return None

# ========================
# ダミー run_fc_registration（本番は差し替え）
# ========================
from selenium.common.exceptions import NoAlertPresentException, TimeoutException, UnexpectedAlertPresentException
import traceback

def run_fc_registration(user, pwd, headless, session_dir, metadata):
    logger.info("Starting FC registration process")

    if not install_chrome_and_driver():
        raise Exception("Failed to setup Chrome environment")

    options = setup_chrome_options()
    if not headless:
        options.arguments = [arg for arg in options.arguments if arg != "--headless"]

    driver_path = get_chrome_driver_path()
    logger.info(f"Using ChromeDriver path: {driver_path}")

    driver = None
    try:
        service = ChromeService(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 30)
        driver.get(f"{FC_BASE_URL}/login.php")

        logger.info("Logging in...")
        wait.until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(user)
        driver.find_element(By.NAME, "password").send_keys(pwd)
        driver.find_element(By.CLASS_NAME, "login-button").click()

        logger.info("Navigating to registration form")
        driver.get(f"{FC_BASE_URL}/location/?mode=entry")

        def input_field(name, value):
            el = driver.find_element(By.NAME, name)
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            el.clear()
            el.send_keys(value)

        input_field("name_ja", metadata["place"])
        input_field("name_kana", metadata["furigana"])
        input_field("description", metadata["description"])
        input_field("address", metadata["address"])
        input_field("lat", str(metadata["lat"]))
        input_field("lng", str(metadata["lng"]))

        # 位置検索ボタン押下後、lat, lng 自動取得完了を待つ（失敗時Alert）
        try:
            btn_geo = driver.find_element(By.CLASS_NAME, "btn-geo")
            driver.execute_script("arguments[0].click();", btn_geo)
            wait.until(lambda d: d.find_element(By.NAME, "lat").get_attribute("value") != "")
        except UnexpectedAlertPresentException:
            try:
                alert = driver.switch_to.alert
                alert_text = alert.text
                alert.accept()
                raise Exception(f"ジオコーディング失敗: {alert_text}")
            except NoAlertPresentException:
                raise Exception("ジオコーディング失敗: Alert内容取得不可")

        # メイン画像の登録
        if metadata.get("main_file"):
            logger.info("Uploading main image")
            btn_img = driver.find_element(By.CSS_SELECTOR, '[data-target="#modal-img-add"]')
            driver.execute_script("arguments[0].click();", btn_img)
            time.sleep(1)

            img_path = os.path.join(session_dir, metadata["main_file"])
            file_input = wait.until(EC.presence_of_element_located((By.ID, "upload-img")))
            file_input.send_keys(os.path.abspath(img_path))

            time.sleep(2)
            btn_use = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[text()="この画像を使用"]')))
            btn_use.click()

        # 保存
        logger.info("Submitting registration")
        save_btn = driver.find_element(By.ID, "save-btn")
        driver.execute_script("arguments[0].click();", save_btn)

        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "alert-success")))
        logger.info("Registration successful")

    except Exception as e:
        logger.error("FC registration error: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        st.error(f"❌ 自動登録中にエラー発生: {e}")
    finally:
        if driver:
            driver.quit()

# ========================
# メイン関数
# ========================
def main():
    st.set_page_config(page_title="画像圧縮＋自動登録", layout="wide")
    st.title("📷 画像圧縮 + 地名登録 + FC自動登録アプリ")

    st.sidebar.header("🔐 FCログイン情報")
    fc_user = st.sidebar.text_input("FCログインID")
    fc_pass = st.sidebar.text_input("FCパスワード", type="password")
    headless = st.sidebar.checkbox("ヘッドレスモード", value=True)

    st.sidebar.header("📁 Google Drive設定")
    folder_id = st.sidebar.text_input("画像格納フォルダID（Google Drive）")
    if not folder_id:
        st.stop()

    service = get_drive_service()
    files = service.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false",
        fields="files(id, name)"
    ).execute().get("files", [])

    if not files:
        st.warning("画像が見つかりません")
        st.stop()

    st.info(f"{len(files)} 枚の画像が見つかりました")

    place = st.text_input("地名（漢字）")
    furigana = st.text_input("ふりがな", convert_to_furigana(place) if place else "")
    desc = st.text_area("概要", "")

    max_kb = st.sidebar.slider("圧縮最大サイズ (KB)", 100, 2000, 800)
    max_bytes = max_kb * 1024

    st.subheader("🖼️ 画像補正＆選択")
    settings = {}
    os.makedirs("tmp_images", exist_ok=True)
    select_all = st.checkbox("全画像選択")

    for file in files:
        file_id = file["id"]
        name = file["name"]
        path = os.path.join("tmp_images", name)
        content = service.files().get_media(fileId=file_id).execute()
        with open(path, "wb") as f:
            f.write(content)

        img = Image.open(path)
        col1, col2 = st.columns(2)
        col1.image(img, caption="元画像", use_column_width=True)

        b = st.slider(f"明るさ [{name}]", 0.5, 2.0, 1.2, 0.1)
        c = st.slider(f"コントラスト [{name}]", 0.5, 2.0, 1.2, 0.1)
        cl = st.slider(f"彩度 [{name}]", 0.5, 2.0, 1.2, 0.1)

        enhanced = enhance_image(img.copy(), b, c, cl)
        col2.image(enhanced, caption="補正後", use_column_width=True)

        sel = st.checkbox(f"この画像を使用する [{name}]", value=select_all)
        is_main = st.checkbox(f"メイン画像として使用 [{name}]", value=False)

        settings[name] = {
            "selected": sel,
            "main": is_main,
            "b": b,
            "c": c,
            "col": cl
        }

    if st.button("🔁 実行（圧縮＋アップロード＋自動登録）"):
        with st.spinner("処理中..."):
            address, lat, lng = search_location_info(place)
            timestamp_folder_id, timestamp_folder_name = create_timestamped_folder(service, folder_id)
            output_dir = f"compressed_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            os.makedirs(output_dir, exist_ok=True)

            sub_files = []
            main_file = None

            for name, setting in settings.items():
                if not setting["selected"]:
                    continue

                img = Image.open(f"tmp_images/{name}")
                enhanced = enhance_image(img, setting["b"], setting["c"], setting["col"])
                buffer = compress_image(enhanced, max_bytes)

                if buffer:
                    out_name = f"compressed_{name}"
                    out_path = os.path.join(output_dir, out_name)
                    with open(out_path, "wb") as f:
                        f.write(buffer.getvalue())

                    sub_files.append(out_name)
                    if setting["main"]:
                        main_file = out_name

                    media = MediaIoBaseUpload(open(out_path, "rb"), mimetype="image/jpeg")
                    service.files().create(
                        body={"name": out_name, "parents": [timestamp_folder_id]},
                        media_body=media
                    ).execute()

            metadata = {
                "place": place,
                "furigana": furigana,
                "description": desc,
                "address": address,
                "lat": lat,
                "lng": lng,
                "main_file": main_file,
                "sub_files": sub_files
            }

            run_fc_registration(fc_user, fc_pass, headless, output_dir, metadata)

            st.success("✅ 処理が完了しました")

if __name__ == "__main__":
    main()
