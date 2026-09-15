# CLAUDE.md — VCOD 工作約定

給在此專案工作的 AI 的規範。專案總覽 / 啟動 / 架構見 [README.md](README.md)。

## 語言與文件

- 回答與程式碼註解、docstring 一律**繁體中文**。
- 有意義的變更(新增/移除功能、改用法、改目錄或相依)後,同步更新 README.md、
  CLAUDE.md 與相關 docstring。純格式/暫時除錯不必更新文件。
- 完成一段有意義變更後主動 `git commit`,訊息具體說明「改了什麼、為什麼」,
  一個邏輯變更一個 commit。不推送遠端,除非被要求。

## 誠實原則

- 涉及 API/設定/版本等事實,以官方文件為準並附出處,不憑記憶臆測。不確定就說要查證。
- 主張問題前先用直接檢查確認(verify-before-flag);「看起來對」不算驗證。

## 核心架構約定

- **與 WEB_BaselineGuard / WEB_ERS / WEB_ALMS 同款骨架**:FastAPI + SQLAlchemy +
  SQLite、Jinja2 模板(Veeam 式側欄、亮/暗雙主題、零 CDN)、config.json 全域設定、
  AD(NTLM)登入 + RBAC、asyncio 背景迴圈。改版型/配色時與這三個專案保持一致。
- **庫存不入庫**:主機 / VM / 儲存區 / 網路只存在 `inventory.py` 的記憶體快照
  (`Snapshot`),由 `poller.run_round()` 整輪完成後 `inventory.publish()` **整個物件
  替換**;讀取端 `inventory.current()` 拿到的必為一致版本。不要在讀取端就地修改快照。
- **DB 只存五張表**:`vcenters`(連線設定 + 最近輪詢狀態)、`account_roles`、
  `audit_logs`、`alerts`(進行中警示)、`alert_history`(轉態紀錄)。要新增「歷史 /
  趨勢」類功能才考慮把庫存快照落地,並另開表。
- **vsphere.py 只做「打 vCenter、回純 dict」**:`_collect()` 走 PropertyCollector
  批次讀取;新增屬性加進 `_HOST_PROPS / _VM_PROPS / _DS_PROPS` 並在 `_parse_*` 解析,
  **不要**在解析階段回頭存取 MoRef 屬性(每次都是一次 SOAP 往返),關聯一律先建
  `_moId → name` 索引。回傳的 `VcData` 內不得含 pyVmomi 物件。VM 的 `datastores` 取 `VirtualMachine.datastore`
  (含 .vmx / swap / ISO 所在,與 vCenter「資料存放區」分頁一致),`vmdks` / `isos` 另列供提示對照。非 ManagedEntity 的
  物件(如 Alarm 名稱)用 `_collect_objs()` 指定 MoRef 批次讀,且結果跨輪快取。
- **警示引擎約定(alerting.py)**:
  - 規則只在 `RULES` 註冊(key / 層面 / 對象 / usage|state / 預設門檻),評估邏輯集中在
    `evaluate()`;新規則 = 加一個 `Rule` + 在 `evaluate()` 對應區段 `add()`,不要在別處判斷。
    `RULES` / `_FIXED_LEVELS` / `_parse_*` 回傳 dict 勿重複 key(規則頁會渲染兩列同名 checkbox);
    提交前跑 `python -m compileall -q app`,有 ruff 時加 `ruff check --select F601,F602`。
  - 使用者只存「覆寫值」於 `settings.alert_rules`(`rule_config()` 合併預設),規則頁存檔時
    等於預設的值不寫入,避免預設調整後被舊覆寫卡住。
  - 狀態機鍵 `<rule>|<vc_id>::<moid>`;usage 類去抖 N 輪(`alert_debounce_rounds`),
    state 類 1 輪。凍結(vCenter stale)時該座既有狀態**不遞增未命中**;vCenter **停用**時其進行中
    警示下一輪靜默解除(transition 帶 `silent`,歷史記「靜默(vCenter 停用)」,不通知)。
  - 抑制順序:範本 → 維護模式主機(含其 VM)→ 排除樣式 → 主機失聯(其 VM 不評估)。
    Tools 未執行時只發 guest_tools,心跳 / 檔案系統不重複發;Tools 版本過舊另有
    guest_tools_outdated(對應 vCenter「可使用較新版本的 Tools」),心跳 gray 且 Tools 過舊時
    在數值註明可能為原因(gray 常是「果」)。
  - 轉態才落地與通知;`_persist()` 以 key 查詢後更新或新增(記憶體與 DB 不同步時不得炸
    UNIQUE)。重啟 `load_state()` 讀回 firing,視為已通知。
  - 畫面呼吸燈 / 小籤一律取 `alerting.active_index()`(物件 key → 進行中警示),不要在
    模板或檢視函式重算門檻。規則設定改變要反映在 `rules_version()`(納入 partial token)。
  - 名稱衝突:`render()` 的 `active` 是導覽 key,模板變數請用 `items` / `alerts_active`。
  - 深連結:儀表板 / 警示頁的物件連結一律用 `alerting.alert_href(rule, target_type, obj_key)`
    (Jinja global):依規則面向選頁面——VM 的 guest_fs / vm_snapshot_age 到儲存頁、其餘 VM 規則
    到運算頁;帶 `?focus=<vc_id::moid>`(不是名稱——名稱子字串會連帶命中同名 vCenter 的所有
    物件)。卡片 / 列標 `data-key`,`vcodFocus()` 只顯示該物件並展開所屬卡片,使用者輸入搜尋
    即 `vcodClearFocus()`。新增檢視頁請沿用此契約。
  - VM 列小籤依頁面過濾(`inventory.VM_RULES_COMPUTE / VM_RULES_STORAGE`),運算頁不顯示
    容量類、儲存頁不顯示運算類;新增 VM 規則時要決定歸哪一組(可兩組皆列)。
- **跨 vCenter 實體以 `vc_id::moid` 為鍵**(`Snapshot._tag` 補上 `key`),儲存區 /
  Port Group 名稱在不同 vCenter 常重複,勿以名稱合併。
- **單座失敗沿用上一輪**:`_poll_one` 失敗時保留 prev.data 並 `stale=True`,模板以
  「資料過期」badge 呈現;不要讓一座失敗就把它的資料從畫面上抹掉。
- **檢視頁 partial 契約**:`/compute /storage /network /vms` 接 `?partial=1` 只回
  `_panel_*.html`;前端 `vcodAutoRefresh()` 以 innerHTML 替換 `#panel` 後重套搜尋。
  新增檢視頁請沿用:整頁模板 `{% include %}` 面板、`render_partial()` 回片段、
  卡片帶 `data-search` / `data-title`、列帶 `data-search`、可標示文字加 `.hl`。
  partial 請求帶 `since=<views.snapshot_token()>`,token 相同回 204(標頭 X-Snapshot);
  整頁把 token 放在 `#panel[data-snapshot]`。改動會影響卡片外觀的全域設定
  (如警示門檻)要納入 token,否則設定改了畫面不會重畫。
- **運算 / 儲存 / 網路頁依 vCenter 分區段**:路由把卡片交給 `inventory.group_by_vc()`,
  面板模板用 `_vc_group.html` 的 `section` 巨集包每座的 `.grid`;前端 `vcodBindGroups()`
  管收合、`vcodSyncGroups()` 在搜尋 / 隱藏空群組後同步區段計數與整段隱藏。
  新增卡片型檢視頁請沿用此結構,不要回到單一大 grid。
- **呼吸燈單一紅色、只對使用率規則**:`inventory.BREATHE_RULES`(host_cpu / host_mem /
  ds_usage)達警告門檻即紅框呼吸;其他警示(硬體健康、失聯、無法存取、VM 層)只顯示小籤。
  `.vcard.attention-critical / -warning` 都是紅色(使用者決定不以橘色區分等級)。
- **呼吸燈不受 prefers-reduced-motion 限制**:`.vcard.attention-*` 的光暈動畫是明暗變化
  而非位移,維運機常關閉 Windows 動畫效果,若依偏好停用警示會消失(UAT 實測)。
  不要再加回 `@media (prefers-reduced-motion)` 的 `animation:none`。
- **用量色階單一定義**:`webutil.usage_tier()`(u1–u5)對應 base.html 的 `.pbar.u* /
  .tier-u*`;模板一律 `|usage_tier`,勿在模板重寫門檻鏈。色階是視覺輔助、固定不變;
  警示與呼吸燈由引擎規則決定,兩者不要混用。
- **寫入操作三道關**在 `main.require_login` middleware:登入 → 角色(非 GET 需
  full_admin,`/logout` 例外)→ CSRF(`_csrf` 欄位或 `X-CSRF-Token` 標頭)。
  新增表單記得放 `<input type="hidden" name="_csrf" value="{{ csrf_token }}">`,
  AJAX 帶 `window.VCOD_CSRF`。角色由 middleware 每請求以 `auth.roles_for_session()`(執行緒內,
  10 秒 TTL)解析後放 `request.state.roles`,`render()` / `role_flags()` 只讀該值;分權變更後呼叫
  `invalidate_roles_cache()`。本機 admin 以 session `local: True` 辨識,AD 帳號即使叫 admin 也走分權表。
- **本機 admin 密碼**:預設空;啟動時空且 AD 未啟用 → `main._bootstrap_local_admin()` 產生隨機初始密碼
  (密文入 config.json、明文寫 `data/initial_admin_password.txt`、標 `local_admin_initial`)。登入時
  `auth.local_admin_must_change()` 為真(初始或不符 `admin_password_problem()`,≥12 字元、不得為 admin)
  → session `must_change_pw`,middleware 只放行 `/settings` 與 `/logout`。改密碼一律經
  `auth.set_local_admin_password()`(設定頁 / CLI 共用:落地、清旗標、刪初始檔)。不要用「拒絕啟動」
  處理弱密碼,使用者要能進 UI 改。
  登入失敗節流在 `login_guard.py`(帳號 5 / IP 20 次 → 鎖 15 分鐘),成功只清帳號計數。
- **CSP script-src 無 'unsafe-inline'**:模板 `<script>` 一律 `<script nonce="{{ csp_nonce }}">`
  (`render()` 注入;不經 `render()` 的頁面如 login 自行從 `request.state.csp_nonce` 傳),禁止
  `onclick=` / `onchange=` / `onsubmit=` 屬性與 `javascript:` URL,改用 base.html 的事件委派
  (`data-autosubmit`、`data-confirm`、id 綑綁)。`behind_tls` 為真時 cookie Secure + HSTS。
- **secret 落地一律密文**:config.json 的欄位列在 `config._ENC_FIELDS`,DB 欄位用
  `EncryptedStr`;新增密碼類欄位務必登記,並在設定頁「留空 = 不變更」(勿把明碼
  render 進 HTML)。
- **沿用已存密碼的測試必須綁定目標**(`/vcenters/test`、`/settings/ad-test`):
  密碼欄留空時,主機 / 埠 / 帳號(vCenter 連 `verify_ssl`)須與已儲存值相同,否則拒絕並記
  `*_blocked` 稽核。編輯 vCenter 變更主機 / 埠 / 帳號而未重輸密碼同樣拒絕(`vcenter_edit_blocked`)。
- **同步阻塞 I/O 一律丟執行緒**:只查 DB / 渲染模板的路由直接寫成同步 `def`(FastAPI 丟
  threadpool);需要 `await` 的路由才用 `async def`,其中 pyVmomi、ldap3、SQLite 查表用
  `asyncio.to_thread`。`audit()` 在 event loop 內自動丟 executor;`poller.request_poll()`
  可自任何執行緒呼叫(`call_soon_threadsafe`)。
- **時間**:取「現在」一律 `config.local_now()`(settings.timezone,naive),aware datetime 轉本地
  一律 `config.to_local()`;勿混用 `datetime.now()` / `astimezone()`(系統時區)。
- **設定優先序**:環境變數 `VCOD_*` > config.json > .env;`start_vcod.cmd` 啟動時以同一 `settings` 取埠。
- **懸浮提示一律寫 `title`,由 base.html 的 `#vtip` 接管**(原生 title 延遲約 1 秒、無法排版,
  舊版 NiceGUI 的 Quasar tooltip 觀感較佳,故以零依賴浮層重現):首次懸浮把 `title` 搬到 `data-tip`
  壓掉原生提示;`&#10;` 分行、多行時首行粗體(標題)、空行(`&#10;&#10;`)渲染為分隔線、兩個空白
  開頭縮排、`[ds] 路徑` / `C:\` / `MAC:` 開頭自動等寬;逐條同級的清單(警示小籤、Guest 磁碟)
  加 `data-tip-flat` 取消首行粗體。partial 換入後呼叫 `window.vcodTipHide()`。不要另寫 tooltip 元件,
  也不要把 HTML 塞進 title(浮層以 textContent 渲染)。
- **前端衛生**:伺服器回傳文字進 DOM 一律 `textContent` / 文字節點,只有
  `#panel` 的 partial(本站 Jinja 自動轉義輸出)可用 innerHTML;不得引用外部
  CDN(CSP 也會擋)。

## 相依與版本

- `requirements.txt` 只表達下限,部署一律 `pip install -r requirements.lock.txt`;
  升級流程:改下限 → 測試環境驗證 → `pip freeze` 重產 lock → commit。
- pyVmomi 9.x:`from pyVim.connect import SmartConnect, Disconnect` 仍為正式入口;
  `SmartConnect(..., sslContext=, httpConnectionTimeout=)`。

## 測試

- 警示引擎:以合成 dict 組 `Snapshot` 連跑 `alerting.run_round()`,把 `alerting.SessionLocal`
  指到暫存 SQLite、monkeypatch `notify.send` 收集訊息;驗證去抖輪數、狀態類立即、抑制 /
  凍結、恢復、等級變更、重啟不重發、通知截斷(開發紀錄中的 test_alerting.py 流程)。

- 無法連真 vCenter 時,以 `vim.*` 資料物件合成 `raw` dict 直接餵 `vsphere._parse_*`
  (pyVmomi 的資料類別可離線實例化),再組 `Snapshot` 驗證 `inventory.*_view` 與
  模板渲染(見開發紀錄中的合成測試流程)。
- 端到端:`VCOD_WEB_PORT=<測試埠> python run.py`,以 curl 走登入 → 各頁 200 →
  無 CSRF 的 POST 403 → 有 CSRF 的 POST 303。測試完刪除 `data/vcod.db`。
