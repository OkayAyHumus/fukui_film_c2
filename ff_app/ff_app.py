
import streamlit as st
import requests
import traceback
import logging
import os
import tempfile
import chromedriver_autoinstaller
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import UnexpectedAlertPresentException, NoAlertPresentException

# ログ設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def search_location_info(place_name):
    try:
        key = st.secrets["google_maps"]["api_key"]
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={place_name}&language=ja&key={key}"
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("status") != "OK":
            logger.warning(f"Geocoding failed for '{place_name}': {data.get('status')}")
            return "", "", ""
        r = data["results"][0]
        logger.info(f"Geocoding successful for '{place_name}'")
        return r["formatted_address"], r["geometry"]["location"]["lat"], r["geometry"]["location"]["lng"]
    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        return "", "", ""

def get_chrome_driver_path():
    temp_dir = os.path.join(tempfile.gettempdir(), "chromedriver")
    os.makedirs(temp_dir, exist_ok=True)
    driver_path = chromedriver_autoinstaller.install(path=temp_dir)
    logger.info(f"ChromeDriver installed at: {driver_path}")
    return driver_path

def setup_chrome_options(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")
    return options

def main():
    st.set_page_config(page_title="画像圧縮＋地名情報取得", layout="wide")
    st.title("📷 画像圧縮＋地名情報取得アプリ")

    place = st.text_input("地名（漢字）")
    furigana = st.text_input("ふりがな")
    desc = st.text_area("概要", "")
    max_kb = st.sidebar.number_input("🔧 圧縮後最大KB", 50, 2048, 2000)
    max_bytes = max_kb * 1024

    if st.button("🔍 圧縮→検索→Drive保存→自動登録"):
        try:
            status_text = st.empty()
            progress_bar = st.progress(0)

            status_text.text("住所情報を検索中...")
            progress_bar.progress(10)
            addr, lat, lng = search_location_info(place)

            if not lat or not lng:
                st.error("⚠️ 入力された地名では位置情報が取得できません。例：『銀座』→ ❌、『東京都中央区銀座1-2-3』→ ✅")
                logger.warning(f"Geocoding returned no result for: {place}")
                st.stop()

            metadata = {
                "place": place,
                "furigana": furigana,
                "description": desc,
                "address": addr,
                "lat": lat,
                "lng": lng
            }

            st.success("ジオコーディング成功")
            st.json(metadata)

            # Chromeテスト起動（必要に応じて）
            driver_path = get_chrome_driver_path()
            options = setup_chrome_options(headless=True)
            service = ChromeService(executable_path=driver_path)
            driver = webdriver.Chrome(service=service, options=options)
            driver.get("https://www.google.com")
            st.success("Chrome起動成功")
            driver.quit()

        except Exception as e:
            st.error(f"❌ エラーが発生しました: {e}")
            st.code(traceback.format_exc())

if __name__ == "__main__":
    main()
