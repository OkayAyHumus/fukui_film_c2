# ff_app.py

import streamlit as st
import pandas as pd
import os
import shutil
import requests
import time
import uuid
import traceback
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

# ========================
# 定数
# ========================
FC_BASE_URL = "https://fc.jl-db.jp"
CHROMEDRIVER_SECRET_KEY = "chromedriver_path"  # st.secrets["selenium"][CHROMEDRIVER_SECRET_KEY]

# ========================
# Google Drive 接続・フォルダ作成
# ========================
def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"]
    )
    return build("drive", "v3", credentials=creds)

def create_timestamped_folder(service, parent_id):
    name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    meta = {"name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents":[parent_id]}
    fid = service.files().create(body=meta, fields="id").execute()["id"]
    return fid, name

# ========================
# users.csv の読み込み
# ========================
@st.cache_data
def load_users(_service, admin_folder_id):
    q = f"'{admin_folder_id}' in parents and name='users.csv' and mimeType='text/csv'"
    files = _service.files().list(q=q, fields="files(id)").execute().get("files",[])
    if not files:
        return None, None
    fid = files[0]["id"]
    fh = BytesIO()
    downloader = _service.files().get_media(fileId=fid)
    downloader = _service._http.request  # workaround for stubs
    # actually use MediaIoBaseDownload
    from googleapiclient.http import MediaIoBaseDownload
    downloader = MediaIoBaseDownload(fh, _service.files().get_media(fileId=fid))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    return pd.read_csv(fh), fid

# ========================
# ログイン機能
# ========================
def login(users_df):
    st.sidebar.header("🔐 ログイン")
    if "username" in st.session_state:
        st.sidebar.success(f"ログイン中: {st.session_state['username']}")
        if st.sidebar.button("ログアウト"):
            for k in ("username","folder_id","is_admin"): st.session_state.pop(k, None)
            st.sidebar.info("ログアウトしました。")
        return
    u = st.sidebar.text_input("ユーザー名", key="login_user")
    p = st.sidebar.text_input("パスワード", type="password", key="login_pass")
    if st.sidebar.button("ログイン"):
        df = users_df.copy()
        df["username"] = df["username"].str.strip()
        df["password"] = df["password"].str.strip()
        m = df[(df["username"]==u.strip()) & (df["password"]==p.strip())]
        if not m.empty:
            st.session_state["username"] = u.strip()
            st.session_state["folder_id"] = m.iloc[0]["folder_id"]
            st.session_state["is_admin"] = (u.strip()=="admin")
            st.sidebar.success("ログイン成功")
        else:
            st.sidebar.error("認証失敗")

# ========================
# Geocoding + ふりがな変換
# ========================
def search_location_info(place_name):
    key = st.secrets["google_maps"]["api_key"]
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={place_name}&language=ja&key={key}"
    data = requests.get(url).json()
    if data.get("status")!="OK":
        return "", "", ""
    r = data["results"][0]
    return r["formatted_address"], r["geometry"]["location"]["lat"], r["geometry"]["location"]["lng"]

def convert_to_furigana(text):
    k = kakasi()
    k.setMode("H","a"); k.setMode("K","a"); k.setMode("J","a")
    return k.getConverter().do(text)

# ========================
# 画像補正・圧縮
# ========================
def enhance_image(img,b,c,col):
    img = ImageEnhance.Brightness(img).enhance(b)
    img = ImageEnhance.Contrast(img).enhance(c)
    img = ImageEnhance.Color(img).enhance(col)
    return img

def compress_image(img,max_bytes):
    buf = BytesIO(); q=95
    while q>=10:
        buf.seek(0); buf.truncate()
        img.save(buf,format="JPEG",quality=q,optimize=True)
        if buf.tell()<=max_bytes:
            return buf
        q-=5
    return None

# ========================
# FCサイト自動登録
# ======================
def run_fc_registration(user, pwd, headless, session_dir, metadata):
    import os, time
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(
        service=ChromeService(executable_path=st.secrets["selenium"][CHROMEDRIVER_SECRET_KEY]),
        options=options
    )
    wait = WebDriverWait(driver, 40)

    try:
        # 1) ログイン
        driver.get(f"{FC_BASE_URL}/login.php")
        wait.until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(user)
        driver.find_element(By.NAME, "password").send_keys(pwd)
        driver.find_element(By.NAME, "login").click()

        # 2) 新規登録ページへ
        driver.get(f"{FC_BASE_URL}/location/?mode=detail&id=0")
        wait.until(EC.presence_of_element_located((By.NAME, "name_ja")))

        # 2.1) 画像登録モーダルを開いて全画像アップロード
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
        file_input.send_keys("\n".join(paths))

        # アップロードリスト数を待機
        expected_count = len(paths)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#files li.media")) >= expected_count)

        # 完了ステータスが揃うまで無制限ループ
        while True:
            bars = driver.find_elements(By.CSS_SELECTOR, "#files li.media .progress-bar")
            statuses = driver.find_elements(By.CSS_SELECTOR, "#files li.media .status")
            if (len(bars) >= expected_count and len(statuses) >= expected_count
                and all(bar.get_attribute("aria-valuenow") == "100" for bar in bars)
                and all("Complete" in status.text for status in statuses)):
                break
            time.sleep(0.5)

        # モーダルを閉じる
        close_add = driver.find_element(By.CSS_SELECTOR, "#modal-img-add button[data-dismiss='modal']")
        driver.execute_script("arguments[0].click();", close_add)

        # 3) 地名／ふりがな／所在地 入力
        for field_name, value in [
            ("name_ja",    metadata.get("place", "")),
            ("name_kana",  metadata.get("furigana", "")),
            ("place_ja",   metadata.get("address", ""))
        ]:
            el = driver.find_element(By.NAME, field_name)
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            el.clear()
            el.send_keys(value)

        # 4) 緯度経度取得
        btn_geo = driver.find_element(By.ID, "btn-g-search")
        driver.execute_script("arguments[0].scrollIntoView(true);", btn_geo)
        driver.execute_script("arguments[0].click();", btn_geo)
        wait.until(lambda d: d.find_element(By.NAME, "lat").get_attribute("value") != "")

        # 5) 概要
        desc_el = driver.find_element(By.ID, "entry-description-ja")
        driver.execute_script("arguments[0].scrollIntoView(true);", desc_el)
        desc_el.clear()
        desc_el.send_keys(metadata.get("description", ""))

        # 6) 非公開フラグ
        sel = driver.find_element(By.NAME, "activated")
        for opt in sel.find_elements(By.TAG_NAME, "option"):
            if opt.get_attribute("value") == "0":
                driver.execute_script("arguments[0].scrollIntoView(true);", opt)
                opt.click()
                break

        # 7) メイン画像選択
        main_file = metadata.get("main_file")
        if main_file:
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
            # driver.find_element(By.CSS_SELECTOR, "#modal-img-select button[data-dismiss='modal']").click()
            time.sleep(8)

        sub_files = metadata.get("sub_files") or []
        if sub_files:
       

            for fname in sub_files:
                # 「画像選択」ボタンをクリックしてモーダル表示
                btn_sub = wait.until(EC.element_to_be_clickable((By.ID, "select-sub-img")))
                driver.execute_script("arguments[0].scrollIntoView(true);", btn_sub)
                btn_sub.click()
                time.sleep(5)

                # モーダルが開かれ、検索用入力欄が表示されるまで待機
                wait.until(EC.visibility_of_element_located((By.ID, "modal-img-select")))
                time.sleep(5)




                # ① 検索語を入力
                input_search = wait.until(EC.presence_of_element_located((By.ID, "search-file-name")))
                driver.execute_script("arguments[0].scrollIntoView(true);", input_search)
                input_search.clear()
                input_search.send_keys(fname)

                # ② 検索実行ボタンをクリック
                btn_search = driver.find_element(By.ID, "search-img")
                driver.execute_script("arguments[0].scrollIntoView(true);", btn_search)
                btn_search.click()

                # ③ 検索結果が返ってくるのを待機
                #    `.select-img-box` が少なくとも 1 件表示されるまで待つ
                wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#modal-img-select .select-img-box")))
                time.sleep(8)

                # ④ 一件目の「選択」ボタンをクリック
                first_box = driver.find_elements(By.CSS_SELECTOR, "#modal-img-select .select-img-box")[0]
                link = first_box.find_element(By.CSS_SELECTOR, "a.select-img-vw")
                driver.execute_script("arguments[0].scrollIntoView(true);", link)
                link.click()

                # 検索語をクリアして、次の周辺画像の検索に備える
                input_search.clear()
                time.sleep(5)

            # ⑤ 全件選択が終わったら「閉じる」ボタンをクリック
            # close_sub = driver.find_element(By.CSS_SELECTOR, "#modal-img-select button[data-dismiss='modal']")
            # driver.execute_script("arguments[0].click();", close_sub)


        # # モーダル内のチェックボックスが表示されるまで待機
        # wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "input.category-modal-select")))
        # boxes = driver.find_elements(By.CSS_SELECTOR, "input.category-modal-select")
        # target = next((cb for cb in boxes if cb.get_attribute("value") == "133"), None)
        # if not target and boxes:
        #     target = boxes[0]
        # if target:
        #     driver.execute_script("arguments[0].scrollIntoView(true);", target)
        #     target.click()

        # # モーダルを閉じる
        # close_cat = driver.find_element(By.CSS_SELECTOR, "button[data-dismiss='modal'], .btn-cls")
        # driver.execute_script("arguments[0].click();", close_cat)

        # driver.find_element(By.CSS_SELECTOR, "#modal-img-select button[data-dismiss='modal']").click()

        # 9) カテゴリ選択
        btn_cat =wait.until(EC.element_to_be_clickable((By.ID, "select-category-btn")))
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
        # driver.find_element(By.CSS_SELECTOR, "button[data-dismiss='modal'], .btn-cls").click()

        # 10) 保存
        save_btn = wait.until(EC.element_to_be_clickable((By.ID, "save-btn")))
        driver.execute_script("arguments[0].scrollIntoView(true);", save_btn)
        driver.execute_script("arguments[0].click();", save_btn)
        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".alert-success")))

    except Exception as e:
        st.error(f"❌ 自動登録中にエラーが発生しました: {e}")
        raise

    finally:
        if headless:
            driver.quit()
        else:
            st.info("ヘッドレスOFF のため、ブラウザが開いたままです。")

# ========================
# メイン
# ========================
def main():
    st.set_page_config(page_title="画像圧縮＋地名情報取得", layout="wide")
    st.title("📷 画像圧縮＋地名情報取得アプリ")

    # Drive & users
    service = get_drive_service()
    users_df,_ = load_users(service, st.secrets["folders"]["admin_folder_id"])
    if users_df is None:
        st.error("users.csv が見つかりません"); return

    login(users_df)
    if "username" not in st.session_state: st.stop()

    # FC-site 設定
    st.sidebar.header("⚙️ FCサイト設定")
    fc_user = st.sidebar.text_input("FC ログインID")
    fc_pass = st.sidebar.text_input("FC パスワード", type="password")
    headless= st.sidebar.checkbox("ヘッドレス実行",value=True)

    # Drive 画像フォルダ
    folder_id = st.text_input("📁 Google Drive フォルダIDを入力")
    if not folder_id: st.stop()
    files = service.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
        fields="files(id,name)"
    ).execute().get("files",[])
    if not files: st.warning("画像が見つかりません"); return

    # 基本情報
    place    = st.text_input("地名（漢字）")
    furigana = st.text_input("ふりがな")
    desc     = st.text_area("概要","")
    max_kb   = st.sidebar.number_input("🔧 圧縮後最大KB",50,2048,2000)
    max_bytes= max_kb * 1024

    # 画像プレビュー＆設定
    st.header("🖼️ 画像選択・補正")
    select_all=st.checkbox("すべて選択")
    settings={}
    os.makedirs("data",exist_ok=True)
    for f in files:
        fid,name = f["id"],f["name"]
        path=os.path.join("data",name)
        with open(path,"wb") as fp: fp.write(service.files().get_media(fileId=fid).execute())
        img=Image.open(path)
        b=st.slider(f"明るさ[{name}]",0.5,2.0,1.2,0.1,key=f"b_{name}")
        c=st.slider(f"コントラスト[{name}]",0.5,2.0,1.2,0.1,key=f"c_{name}")
        col=st.slider(f"彩度[{name}]",0.5,2.0,1.3,0.1,key=f"col_{name}")
        en=enhance_image(img.copy(),b,c,col)
        c1,c2=st.columns(2)
        with c1: st.image(img,caption="元",use_container_width=True)
        with c2: st.image(en,caption="補正",use_container_width=True)
        main=st.checkbox("メインで使う",key=f"main_{name}")
        sel=st.checkbox("選択",key=f"sel_{name}",value=select_all)
        settings[name]={"b":b,"c":c,"col":col,"main":main,"sel":sel}

    if st.button("🔍 圧縮→検索→Drive保存→自動登録"):
        # 一時ディレクトリ
        session_dir=f"output/{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        os.makedirs(session_dir,exist_ok=True)

        # 住所検索
        addr,lat,lng=search_location_info(place)
        metadata={"place":place,"furigana":furigana,"description":desc,
                  "address":addr,"lat":lat,"lng":lng}

        # 圧縮＆ファイルリスト
        sub_files=[]; main_file=None
        for f in files:
            name=f["name"]; s=settings[name]
            if not s["sel"]: continue
            img=Image.open(os.path.join("data",name))
            en=enhance_image(img,s["b"],s["c"],s["col"])
            buf=compress_image(en,max_bytes)
            out=f"compressed_{name}"
            if buf:
                with open(os.path.join(session_dir,out),"wb") as fp: fp.write(buf.getvalue())
                sub_files.append(out)
                if s["main"]: main_file=out

        metadata["main_file"]=main_file
        metadata["sub_files"]=sub_files

        # CSV 作成
        csv_path=os.path.join(session_dir,"metadata.csv")
        pd.DataFrame([metadata]).to_csv(csv_path,index=False)

        # Google Drive にチャンクアップロード
        new_fid,new_name=create_timestamped_folder(service,folder_id)
        st.info(f"▶ アップロード先: {new_name}")
        for fn in os.listdir(session_dir):
            fp=os.path.join(session_dir,fn)
            mime="image/jpeg" if fn.lower().endswith((".jpg",".jpeg")) else "text/csv"
            media=MediaIoBaseUpload(open(fp,"rb"),mimetype=mime,
                                   resumable=True, chunksize=1024*1024)
            req=service.files().create(body={"name":fn,"parents":[new_fid]},
                                       media_body=media)
            uploaded=False
            with st.spinner(f"Uploading {fn}..."):
                while not uploaded:
                    status,resp=req.next_chunk()
                    if status:
                        st.write(f"  {fn}: {int(status.progress()*100)}%")
                    if resp:
                        uploaded=True
            st.success(f"  ✅ {fn} uploaded")

        st.success("🎉 Drive へのアップロード完了")

        # FC 自動登録
        try:
            run_fc_registration(fc_user,fc_pass,headless,session_dir,metadata)
            st.success("✅ FCサイト自動登録完了")
        except Exception:
            st.error("❌ 自動登録中にエラー発生")

        # ローカル削除
        shutil.rmtree(session_dir)

if __name__=="__main__":
    main()
