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
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-images")
    options.add_argument("--disable-javascript")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--memory-pressure-off")
    options.add_argument("--max_old_space_size=4096")
    options.add_argument("--disable-web-security")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--ignore-certificate-errors-spki-list")
    return options

def get_chrome_driver_path():
    try:
        import chromedriver_binary
        return chromedriver_binary.chromedriver_filename
    except ImportError:
        if "chromedriver_path" in st.secrets.get("selenium", {}):
            return st.secrets["selenium"]["chromedriver_path"]
        else:
            return "chromedriver"

def install_chrome_and_driver():
    try:
        import chromedriver_binary
        logger.info("chromedriver-binary is already installed")
        return True
    except ImportError:
        logger.warning("chromedriver-binary not found. Please install it via requirements.txt")
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
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }
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

        # 2) 新規登録ページへ遷移
        logger.info("Step 2: Navigating to registration page")
        driver.get(f"{FC_BASE_URL}/location/?mode=detail&id=0")
        wait.until(EC.presence_of_element_located((By.NAME, "name_ja")))

        # 2.1) 画像登録モーダルを開いて画像アップロード
        logger.info("Step 2.1: Opening image upload modal")
        btn_add = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-toggle='modal'][data-target='#modal-img-add']")))
        driver.execute_script("arguments[0].click();", btn_add)

        file_input = wait.until(EC.presence_of_element_located((By.ID, "InputFile")))

        # 圧縮済み画像ファイルを取得
        paths = [
            os.path.abspath(os.path.join(session_dir, fn))
            for fn in os.listdir(session_dir)
            if fn.startswith("compressed_") and fn.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
        logger.info(f"Uploading {len(paths)} images")
        file_input.send_keys("\n".join(paths))

        # アップロード完了を待機
        expected_count = len(paths)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#files li.media")) >= expected_count)

        logger.info("Waiting for image uploads to complete")
        while True:
            bars = driver.find_elements(By.CSS_SELECTOR, "#files li.media .progress-bar")
            statuses = driver.find_elements(By.CSS_SELECTOR, "#files li.media .status")
            if (
                len(bars) >= expected_count and len(statuses) >= expected_count and
                all(bar.get_attribute("aria-valuenow") == "100" for bar in bars) and
                all("Complete" in status.text for status in statuses)
            ):
                break
            time.sleep(3)

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
        driver.execute_script("arguments[0].click();", btn_geo)
        wait.until(lambda d: d.find_element(By.NAME, "lat").get_attribute("value") != "")

        # 5) 概要入力
        logger.info("Step 5: Filling description")
        desc_el = driver.find_element(By.ID, "entry-description-ja")
        desc_el.clear()
        desc_el.send_keys(metadata.get("description", ""))

        # 6) 公開状態を非公開に変更
        logger.info("Step 6: Setting privacy flag")
        sel = driver.find_element(By.NAME, "activated")
        for opt in sel.find_elements(By.TAG_NAME, "option"):
            if opt.get_attribute("value") == "0":
                opt.click()
                break

        # 7) メイン画像選択
        main_file = metadata.get("main_file")
        if main_file:
            logger.info(f"Step 7: Setting main image: {main_file}")
            btn_main = wait.until(EC.element_to_be_clickable((By.ID, "select-main-img")))
            driver.execute_script("arguments[0].click();", btn_main)
            wait.until(EC.visibility_of_element_located((By.ID, "modal-img-select")))
            time.sleep(2)

            for box in driver.find_elements(By.CSS_SELECTOR, "#modal-img-select .select-img-box"):
                if main_file in box.text:
                    link = box.find_element(By.CSS_SELECTOR, "a.select-img-vw")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                    time.sleep(2)
                    driver.execute_script("arguments[0].click();", link)
                    break
            time.sleep(5)

        # 8) サブ画像選択
        sub_files = metadata.get("sub_files") or []
        if sub_files:
            logger.info(f"Step 8: Setting sub images: {len(sub_files)} files")
            for fname in sub_files:
                if fname == main_file:
                    continue
                logger.info(f"Processing sub image: {fname}")

                btn_sub = wait.until(EC.element_to_be_clickable((By.ID, "select-sub-img")))
                driver.execute_script("arguments[0].click();", btn_sub)
                time.sleep(3)

                wait.until(EC.visibility_of_element_located((By.ID, "modal-img-select")))
                input_search = wait.until(EC.presence_of_element_located((By.ID, "search-file-name")))
                input_search.clear()
                input_search.send_keys(fname)

                btn_search = driver.find_element(By.ID, "search-img")
                btn_search.click()
                wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#modal-img-select .select-img-box")))
                time.sleep(3)

                first_box = driver.find_elements(By.CSS_SELECTOR, "#modal-img-select .select-img-box")[0]
                link = first_box.find_element(By.CSS_SELECTOR, "a.select-img-vw")
                driver.execute_script("arguments[0].click();", link)
                time.sleep(3)

        # 9) カテゴリ選択
        logger.info("Step 9: Setting category")
        btn_cat = wait.until(EC.element_to_be_clickable((By.ID, "select-category-btn")))
        driver.execute_script("arguments[0].click();", btn_cat)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "input.category-modal-select")))
        time.sleep(2)

        cbs = driver.find_elements(By.CSS_SELECTOR, "input.category-modal-select")
        target = next((cb for cb in cbs if cb.get_attribute("value") == "133"), None)
        if not target and cbs:
            target = cbs[0]
        if target:
            driver.execute_script("arguments[0].click();", target)

        # 10) 保存
        logger.info("Step 10: Saving registration")
        save_btn = wait.until(EC.element_to_be_clickable((By.ID, "save-btn")))
        driver.execute_script("arguments[0].click();", save_btn)
        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".alert-success")))

        logger.info("✅ FC registration completed successfully")

    except Exception as e:
        logger.error(f"❌ FC registration error: {e}")
        logger.error(traceback.format_exc())
        raise

    finally:
        if driver:
            if headless:
                driver.quit()
                logger.info("Chrome driver closed")
            else:
                logger.info("ヘッドレスOFF のためブラウザが開いたままです。")

def main():
    st.set_page_config(page_title="画像圧縮＋地名情報取得", layout="wide")
    st.title("📷 画像圧縮＋地名情報取得アプリ")

    # Google Drive認証と users.csv 読み込み
    try:
        logger.info("Initializing Google Drive service")
        service = get_drive_service()
        users_df, _ = load_users(service, st.secrets["folders"]["admin_folder_id"])

        if users_df is None:
            st.error("users.csv が見つかりません")
            return

        login(users_df)
        if "username" not in st.session_state:
            st.stop()

        # サイドバーにログイン者名・設定
        st.sidebar.header("⚙️ FCサイト設定")
        fc_user = st.sidebar.text_input("FC ログインID")
        fc_pass = st.sidebar.text_input("FC パスワード", type="password")
        headless = st.sidebar.checkbox("ヘッドレス実行", value=True)

        # フォルダIDを指定
        folder_id = st.text_input("📁 Google Drive フォルダIDを入力")
        if not folder_id:
            st.stop()

        # 対象画像の取得
        logger.info(f"Loading images from folder: {folder_id}")
        files = service.files().list(
            q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
            fields="files(id, name)"
        ).execute().get("files", [])

        if not files:
            st.warning("画像が見つかりません")
            return

        # 地名などの情報を入力
        st.header("📝 メタデータ入力")
        place = st.text_input("地名（漢字）")
        furigana = st.text_input("ふりがな", convert_to_furigana(place) if place else "")
        desc = st.text_area("概要", "")
        max_kb = st.sidebar.number_input("🔧 圧縮後最大KB", 50, 2048, 2000)
        max_bytes = max_kb * 1024

        # 画像処理用UIと圧縮設定
        st.header("🖼️ 画像選択・補正")
        settings = {}
        select_all = st.checkbox("全画像を選択")
        os.makedirs("tmp_images", exist_ok=True)

        for f in files:
            fid, name = f["id"], f["name"]
            path = os.path.join("tmp_images", name)

            try:
                with open(path, "wb") as fp:
                    fp.write(service.files().get_media(fileId=fid).execute())

                img = Image.open(path)
                b = st.slider(f"明るさ [{name}]", 0.5, 2.0, 1.2, 0.1, key=f"b_{name}")
                c = st.slider(f"コントラスト [{name}]", 0.5, 2.0, 1.2, 0.1, key=f"c_{name}")
                col = st.slider(f"彩度 [{name}]", 0.5, 2.0, 1.3, 0.1, key=f"col_{name}")

                en = enhance_image(img.copy(), b, c, col)

                col1, col2 = st.columns(2)
                with col1:
                    st.image(img, caption="元画像", use_container_width=True)
                with col2:
                    st.image(en, caption="補正後", use_container_width=True)

                main = st.checkbox("メイン画像に設定", key=f"main_{name}")
                sel = st.checkbox("この画像を使う", key=f"sel_{name}", value=select_all)

                settings[name] = {"b": b, "c": c, "col": col, "main": main, "sel": sel}

            except Exception as e:
                st.error(f"{name} の画像処理中にエラー: {e}")

        # 実行ボタン
        if st.button("📤 圧縮＋検索＋Drive保存＋FC登録"):
            try:
                status_text = st.empty()
                progress_bar = st.progress(0)

                # ① Geocoding
                status_text.text("① 住所情報を取得中...")
                address, lat, lng = search_location_info(place)
                metadata = {
                    "place": place,
                    "furigana": furigana,
                    "description": desc,
                    "address": address,
                    "lat": lat,
                    "lng": lng
                }
                progress_bar.progress(10)

                # ② 画像圧縮
                status_text.text("② 画像を圧縮中...")
                output_dir = f"output_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                os.makedirs(output_dir, exist_ok=True)

                sub_files = []
                main_file = None

                for f in files:
                    name = f["name"]
                    if not settings[name]["sel"]:
                        continue

                    img_path = os.path.join("tmp_images", name)
                    img = Image.open(img_path)
                    en = enhance_image(img, settings[name]["b"], settings[name]["c"], settings[name]["col"])
                    buf = compress_image(en, max_bytes)

                    if buf:
                        out_path = os.path.join(output_dir, f"compressed_{name}")
                        with open(out_path, "wb") as outf:
                            outf.write(buf.getvalue())
                        sub_files.append(f"compressed_{name}")
                        if settings[name]["main"]:
                            main_file = f"compressed_{name}"

                metadata["main_file"] = main_file
                metadata["sub_files"] = sub_files
                progress_bar.progress(40)

                # ③ メタ情報CSVを保存
                df = pd.DataFrame([metadata])
                df.to_csv(os.path.join(output_dir, "metadata.csv"), index=False)
                progress_bar.progress(50)

                # ④ Google Drive へアップロード
                status_text.text("③ Google Drive にアップロード中...")
                new_folder_id, _ = create_timestamped_folder(service, folder_id)
                for fname in os.listdir(output_dir):
                    filepath = os.path.join(output_dir, fname)
                    mime = "image/jpeg" if fname.endswith(".jpg") or fname.endswith(".jpeg") else "text/csv"
                    media = MediaIoBaseUpload(open(filepath, "rb"), mimetype=mime)
                    service.files().create(
                        body={"name": fname, "parents": [new_folder_id]},
                        media_body=media
                    ).execute()
                progress_bar.progress(70)

                # ⑤ FC自動登録
                if fc_user and fc_pass:
                    status_text.text("④ FCサイトに自動登録中...")
                    run_fc_registration(fc_user, fc_pass, headless, output_dir, metadata)
                    st.success("✅ FCサイト登録が完了しました")
                else:
                    st.warning("⚠️ FCログイン情報が未入力のため登録をスキップしました")
                progress_bar.progress(100)
                status_text.text("🎉 完了しました")

            except Exception as e:
                st.error(f"❌ エラー: {e}")
                logger.error(traceback.format_exc())

            finally:
                shutil.rmtree(output_dir, ignore_errors=True)
                shutil.rmtree("tmp_images", ignore_errors=True)

    except Exception as e:
        st.error(f"❌ 初期化エラー: {e}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    main()
