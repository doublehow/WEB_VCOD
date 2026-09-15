# VCOD — vCenter Overview Dashboard

多座 **VMware vCenter** 的維運總覽儀表板 + 警示引擎:背景輪詢各 vCenter 的主機 /
VM / 儲存區 / 網路庫存與即時用量,以瀏覽器呈現運算、儲存、網路、VM 總覽四種檢視;
內建兩層(虛擬平台層 / 客體 OS 層)共 17 條規則的警示引擎,去抖後以 Telegram / SMTP
通知並在畫面呼吸燈標示,免裝任何用戶端。給機房 / 虛擬化維運人員每日巡檢與值班用。

> 內部代號 `vcod`:環境變數 `VCOD_*`、資料庫檔名 `vcod.db`、session cookie
> `vcod_session`、log 名稱 `vcod.*` 皆沿用此代號。
> 本版為舊版(NiceGUI + pyVmomi 單檔程式,v1.4)的全面重製,改採與
> WEB_BaselineGuard / WEB_ERS / WEB_ALMS 同款骨架與版型。

## 監控對象與資料來源

| 對象 | 取自(vSphere Web Services API) | 呈現 |
|---|---|---|
| ESXi 主機 | `HostSystem` summary(hardware / quickStats / runtime / config.product) | CPU / 記憶體使用率、型號、處理器、核心數、版本、運作天數、叢集、連線 / 維護狀態 |
| 虛擬機 | `VirtualMachine` config.hardware(NIC / 磁碟)、runtime(maxCpuUsage)、quickStats(Ready / balloon / swap / 心跳)、snapshot、guest(disk / kernelCrashed)、summary.storage | 電源、vCPU / RAM 配置與使用、VM CPU% / Ready%、每張 NIC 的類型 / MAC / Port Group / IP / SR-IOV、VMDK、Datastore 佔用 / 佈建量、**Guest 檔案系統用量(Tools)**、快照數 / 最舊天數、範本標記 |
| 儲存區 | `Datastore` summary + host 掛載清單 | 容量 / 已用 / 剩餘 / 佈建量(超額佈建標示)、類型、可存取性、掛載主機數、使用的 VM |
| 網路 | `Network` / `DistributedVirtualPortgroup` / `OpaqueNetwork` | 每個 Port Group 上的 VM、電源、該 NIC 的 IP(含無 VM 的空群組) |
| vCenter 告警 | 主機 / VM / 儲存區的 `triggeredAlarmState` + `Alarm.info.name` | vCenter 內建告警中心目前黃 / 紅的項目(僅顯示,可選外送) |

全部**唯讀**:只用 PropertyCollector 讀取,不對 vCenter 做任何變更;
帳號給 vCenter 內建 **Read-only** 角色即可。

## 功能特性

| 頁面 | 說明 |
|---|---|
| 儀表板 | 進行中警示(引擎狀態,嚴重 / 警告數與持續時間)、vCenter 連線狀態表、vCenter 連線數 / 主機 / VM / 儲存區統計、整體 CPU / 記憶體 / 儲存用量、VM 電源甜甜圈、主機 CPU / 記憶體 / 儲存區用量 Top |
| 警示 | 三分頁:**進行中**(等級 / 層面 / 物件 / 規則 / 數值 / 持續,含 vCenter 內建告警區)、**歷史**(觸發 / 恢復 / 等級變更轉態紀錄與通知結果)、**規則**(17 條啟停與門檻、去抖輪數、排除樣式、恢復通知、內建告警外送) |
| 運算 | 以 ESXi 主機為卡片:CPU / 記憶體五段色階長條 + 主機資訊 + 所屬 VM(開機優先、CPU 用量排序);CPU / 記憶體達警告門檻(預設 80%)即紅色呼吸燈;所有進行中警示以小籤(⚠️ 警告 / 🔴 嚴重)列出 |
| 儲存 | 以 Datastore 為卡片:容量長條、剩餘 / 佈建量、使用中的 VM(Datastore 佔用排序、跨儲存區標記、Guest 最高掛載點 %,懸停列出各掛載點);有警示者呼吸燈 |
| 網路 | 以 Port Group 為卡片:開 / 關機數、每台 VM 在該網路的 IP;懸停顯示 NIC 類型 / MAC / DirectPath I/O;可隱藏無 VM 的群組 |
| VM 總覽 | 全部 VM 表格:欄位排序、每頁 25 / 100 / 全部、多值欄位逐行顯示、**Guest 用量**欄(已用 / 容量、最高掛載點長條)、快照與警示小籤、一鍵匯出 CSV(UTF-8 BOM,公式注入消毒,含 Guest 用量 / 快照 / 警示欄) |
| vCenter 管理 | 新增 / 編輯 / 刪除 / 啟停連線、連線測試(登入前即時驗證)、立即更新;顯示各座版本、主機 / VM 數、最後輪詢與錯誤 |
| 稽核紀錄 | 登入 / 設定 / vCenter 異動 / 權限拒絕等操作軌跡(帳號、來源 IP、篩選) |
| 設定 | 輪詢間隔、紀錄保留、**通知管道(Telegram / SMTP,含測試按鈕)**、AD(NTLM)登入(含逐步連線測試)、本機管理員密碼、帳號分權 |

運算 / 儲存 / 網路三頁**依 vCenter 分區段**呈現:每座一個可收合的區段標頭(名稱、連線狀態、
數量、版本),本輪連線失敗的 vCenter 直接在該區段顯示錯誤;收合狀態記在瀏覽器。

四個檢視頁皆有**即時搜尋**(卡片標題命中顯示全部、否則只留命中的 VM 列,
關鍵字 `<mark>` 標示)與**自動更新**(依輪詢間隔以 `?partial=1` 取回面板片段
原地替換,搜尋框 / 捲動位置不受影響;可關閉,分頁隱藏時暫停)。自動更新帶上次快照
token(`since=`),快照未變伺服器回 204 不重繪;全站回應 gzip 壓縮(面板片段約 4–7%)。

## 警示引擎

每輪快照發布後評估;**用量類**規則連續 N 輪(預設 3,可設)命中才觸發、N 輪未命中才解除,
**狀態類**(二元事實)1 輪即觸發。兩級嚴重度(警告 / 嚴重),同一規則可設兩個門檻;
firing 中等級改變記「等級變更」。

| 層面 | 規則 | 警告 | 嚴重 | 資料 |
|---|---|---|---|---|
| 平台 | vCenter 連線失敗 | — | 立即 | 輪詢結果 |
| 平台 | 主機失聯(disconnected / notResponding) | — | 立即 | runtime.connectionState |
| 平台 | 主機 CPU / 記憶體使用率 | 80% | 95% | quickStats |
| 平台 | 主機硬體 / 整體健康 | 黃 | 紅 | overallStatus |
| 平台 | 儲存區用量 | 80% | 90% | summary |
| 平台 | 儲存區無法存取 | — | 立即 | summary.accessible |
| 平台 | 儲存區剩餘空間 | ≤ 200 GB | ≤ 50 GB | summary.freeSpace |
| 平台 | VM CPU 使用率(usage ÷ maxCpuUsage) | 90% | — | quickStats + runtime |
| 平台 | VM CPU Ready | 10% | 20% | overallCpuReadiness(7.0+) |
| 平台 | VM 記憶體回收(balloon / swap / compressed > 0) | 命中 | — | quickStats |
| 平台 | VM 快照過久(最舊快照天數) | 7 天 | 30 天 | snapshot |
| 客體 | Guest 檔案系統用量(最高掛載點;排除 tmpfs / 光碟 / 網路掛載、< 1 GB) | 85% | 95% | guest.disk(Tools) |
| 客體 | Guest 心跳異常(Tools 執行中) | gray | red | guestHeartbeatStatus |
| 客體 | VMware Tools 未執行(開機中) | 命中 | — | toolsRunningStatus |
| 客體 | VMware Tools 版本過舊(NeedUpgrade / TooOld / SupportedOld / Blacklisted) | 命中 | — | toolsVersionStatus2 |
| 客體 | Guest Kernel Crash | — | 立即 | guest.guestKernelCrashed |

**抑制與排除**:主機失聯 / 維護模式 → 其 VM 不評估;vCenter 本輪失敗(資料過期)→ 該座凍結
(既有警示維持、不新增不解除);範本 VM 不評估;名稱樣式排除清單(預設 `vCLS-*`,fnmatch)。
被抑制的物件數在儀表板與警示頁標示。

**通知**:每輪轉態合併一則(嚴重 → 警告 → 等級變更 → 恢復,超過 30 行截斷),Telegram + SMTP;
恢復通知可關;持續中不重發;重啟自 `alerts` 表讀回不重發。vCenter 內建告警預設只顯示,
可選「新出現時外送」。畫面呼吸燈以引擎狀態為準:主機 CPU / 記憶體、儲存區用量達警告門檻即紅色呼吸;其他規則
只顯示小籤,等級由小籤區分;與通知、警示頁一致。

## 服務埠

| 埠 | 用途 |
|---|---|
| **8082** | Web UI(登入後操作介面;沿用舊版埠號) |

改埠:環境變數 `VCOD_WEB_PORT` 或 `config.json` 的 `"web_port"`(環境變數優先)後重啟;`start_vcod.cmd` 啟動時讀同一設定。

## 技術架構

| 層 | 選擇 |
|---|---|
| 後端 | Python 3.14 / FastAPI + Uvicorn 單程序 |
| 資料庫 | SQLite(WAL)+ SQLAlchemy 2.x;存 vCenter 連線設定、帳號分權、稽核紀錄、進行中警示(`alerts`)與警示歷史(`alert_history`)。**庫存不入庫**,由 `inventory.py` 於記憶體維護整體替換的一致快照 |
| 警示 | `alerting.py`:規則表 + 每輪評估 + 去抖狀態機 + 抑制 / 排除 + 落地 + 通知;`notify.py` Telegram / SMTP(移植自 BaselineGuard) |
| vCenter 收集 | pyVmomi;ContainerView + PropertyCollector **批次讀取**(每類物件一次往返,舊版逐屬性存取數百台 VM 需數十秒);MoRef 關聯全在本機以 `_moId` 對應;每座長連線、失敗重連;`httpConnectionTimeout=30` 防黑洞 |
| 輪詢 | 內建 asyncio 迴圈,各 vCenter 在執行緒並行抓取,整輪完成才發布快照;單座失敗沿用上一輪資料並標「資料過期」;設定變更 / 立即更新即時喚醒 |
| 前端 | Jinja2 伺服器端渲染,零前端框架、零 CDN;Veeam 式側欄主控台、亮 / 暗主題;圖表為純 CSS(conic-gradient 甜甜圈、長條);搜尋 / 排序 / 分頁 / 自動更新為少量原生 JS(只操作 DOM 文字節點) |
| 驗證 | 本機 admin(首次啟動自動產生初始密碼於 `data/initial_admin_password.txt`,登入後強制變更;`python -m app.set_admin_password` 可重設 / 停用)+ AD 網域登入(ldap3,NTLM,可選 LDAPS 636);角色分權(管理者 / 唯讀),每請求重查分權表(10 秒 TTL 快取);本機 admin 以 session 旗標辨識,AD 上名為 admin 的帳號仍走分權表;登入失敗節流(同帳號 5 次 / 同 IP 20 次於 15 分鐘 → 鎖 15 分鐘) |
| Session | Starlette SessionMiddleware(簽名 cookie `vcod_session`,8 小時;secret 落地於 config 自動生成;config `behind_tls: true` 時 cookie Secure + HSTS);非 GET 一律驗 CSRF token(表單 `_csrf` / 標頭 `X-CSRF-Token`) |
| 設定 | `config.json`(pydantic-settings;`VCOD_*` 環境變數優先於 config.json;敏感欄位 AES-256-GCM 密文,金鑰 `data/secret.key`) |
| 部署 | venv + `run.py`;Windows 可用 `start_vcod.cmd` 搭配工作排程器開機啟動 |

## 目錄結構

```
WEB_VCOD/
├── run.py                  # 進入點:uvicorn 起 Web UI(埠取自設定,預設 8082)
├── start_vcod.cmd          # Windows 工作排程器啟動腳本(logs/ 每日 log,保留 30 天)
├── app/
│   ├── main.py             # FastAPI app、儀表板、登入 / RBAC / CSRF middleware、安全標頭、lifespan
│   ├── config.py           # config.json(pydantic-settings)、VCOD_* 覆寫、secret 加解密、local_now()
│   ├── secret_store.py     # AES-256-GCM 加密欄位(EncryptedStr,金鑰 data/secret.key)
│   ├── set_admin_password.py  # CLI:建立 / 變更 / 停用本機 admin 密碼
│   ├── database.py         # engine、WAL、init_db(create_all + 輕量遷移)
│   ├── models.py           # VCenter / AccountRole / AuditLog / Alert / AlertHistory
│   ├── auth.py             # 本機 admin + AD(NTLM)驗證、resolve_roles()
│   ├── login_guard.py      # 登入失敗節流(帳號 / IP,記憶體計數)
│   ├── audit.py            # 稽核紀錄 helper
│   ├── vsphere.py          # pyVmomi 客戶端:PropertyCollector 批次抓取、解析為 dict、連線測試
│   ├── inventory.py        # 記憶體快照 + 運算 / 儲存 / 網路 / VM / 儀表板檢視(接引擎警示索引)
│   ├── alerting.py         # 警示引擎:17 條規則、去抖狀態機、抑制 / 排除、alerts 落地、通知組稿
│   ├── notify.py           # Telegram / SMTP 通知
│   ├── poller.py           # 背景輪詢迴圈、狀態回寫 DB、每輪呼叫警示引擎、稽核 / 警示歷史清理
│   ├── import_legacy.py    # 自舊版 config.json 匯入 vCenter 與 AD 設定(python -m app.import_legacy)
│   ├── webutil.py          # templates / render / render_partial、用量色階、CSV 匯出
│   ├── routes/             # auth(登入、分權)/ views(四檢視 + CSV)/ alerts / vcenters / logs_routes / settings_routes
│   └── web/templates/      # Jinja2:base + login + 9 頁 + 4 個 _panel_* 面板片段 + _vc_group / _alert_badge 巨集
├── data/                   # SQLite(vcod.db)、secret.key(gitignore,自動建立)
├── logs/                   # start_vcod.cmd 的執行 log(gitignore)
├── config.json             # 全域設定(gitignore,首次啟動自動生成)
├── requirements.txt        # 相依意圖下限
└── requirements.lock.txt   # 部署用精確鎖定(含間接依賴)
```

## 主要路由

Web UI 除登入頁外皆需 session;非 GET 需「管理者」角色 + CSRF token。

| 路由 | 方法 | 說明 |
|---|---|---|
| `/` | GET | 儀表板 |
| `/login` `/logout` | GET/POST | 登入 / 登出(本機或 AD;登出為 POST) |
| `/compute` `/storage` `/network` `/vms` | GET | 四大檢視(`?partial=1` 只回面板片段) |
| `/vms/export.csv` | GET | VM 總覽匯出 |
| `/alerts`(`?tab=active|history|rules`) | GET | 警示頁三分頁 |
| `/alerts/rules` | POST | 儲存規則啟停 / 門檻 / 去抖 / 排除 / 通知選項 |
| `/vcenters` | GET/POST | vCenter 清單 / 新增 |
| `/vcenters/{id}/edit`、`/delete` | GET/POST | 編輯 / 刪除 |
| `/vcenters/test` | POST(JSON) | 連線測試(密碼留空時沿用已存密碼,且目標須與已存相同) |
| `/vcenters/poll` | POST | 立即更新 |
| `/logs` | GET | 稽核紀錄 |
| `/settings`(`/ad-test`、`/notify-test`) | GET/POST | 設定、AD 逐步測試、通知測試 |
| `/roles`、`/roles/{id}/delete` | POST | 帳號分權 |
| `/api/status` | GET | 頂欄指示器(JSON:連線數、最後輪詢) |

## 安裝與啟動

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt   # 部署一律裝 lock
.\.venv\Scripts\python.exe run.py
```

首次啟動時服務自動產生本機 admin 初始密碼,明文寫入 `data/initial_admin_password.txt`(僅擁有者可讀,
啟動 log 亦提示路徑)。瀏覽 `http://<伺服器>:8082`,以 `admin` + 該密碼登入後會被導到設定頁**強制變更**
(至少 12 字元;變更後初始密碼檔自動刪除),之後才能至「vCenter 管理」新增連線或啟用 AD 登入與帳號分權。
自舊版升級且密碼仍為 `admin` 者,登入後同樣強制變更。忘記密碼時在伺服器執行:

```powershell
.\.venv\Scripts\python.exe -m app.set_admin_password            # 重設(互動輸入)
.\.venv\Scripts\python.exe -m app.set_admin_password --disable  # 停用本機登入(需已啟用 AD)
```

前端有 TLS 反向代理時在 config.json 設 `"behind_tls": true`。

自舊版(NiceGUI 版)搬遷:

```powershell
.\.venv\Scripts\python.exe -m app.import_legacy <舊版 config.json>
```

匯入 vCenter 清單(密碼加密入庫、TLS 驗證沿用舊版「略過」)與 AD 設定
(不自動啟用);匯入後請刪除或妥善保管舊版明文 config.json。

> 不要加 `--reload`:輪詢在背景 thread 進行,reload 會中斷抓取;更新程式後手動重啟。

## 資料與安全

- vCenter 密碼、AD service 密碼、本機 admin 密碼、SMTP 密碼、Telegram Bot Token、session secret 一律 AES-256-GCM
  加密落地,金鑰 `data/secret.key` —— **備份 data/ 務必連同金鑰**,遺失金鑰密文
  不可復原,只能重新輸入。
- 對 vCenter **全程唯讀**;TLS 憑證預設驗證,自簽憑證可逐座取消(管理頁明示)。
- 連線測試 / AD 測試沿用已存密碼時,目標主機(含 TLS 驗證選項)須與已存值相同,防止把密碼送往
  攻擊者架設的假伺服器;測試其他目標須一併輸入密碼。編輯 vCenter 變更主機 / 埠 / 帳號時亦須重新輸入密碼。
- 登入失敗訊息統一「帳號或密碼錯誤」,詳細原因只進稽核紀錄;同帳號 5 次 / 同 IP 20 次失敗(15 分鐘內)
  鎖 15 分鐘(記憶體計數,重啟歸零;`login_locked` 稽核);唯讀角色的寫入請求與 CSRF 失敗皆記稽核。
- 回應一律帶 CSP(`script-src` 僅同源 + 每請求 nonce 的內嵌 script,模板不用 `onclick=` 等內嵌事件屬性;
  `style-src` 因大量 `style=` 屬性允許 inline)、`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`;
  `behind_tls` 時加 HSTS;CSV 匯出對 `= + - @` 開頭儲存格加前綴防公式注入。
- 停用某座 vCenter 時,其進行中警示於下一輪靜默解除(警示歷史記「靜默(vCenter 停用)」,不發通知)。
- 稽核紀錄與警示歷史依保留天數每日自動清理(預設 365 天,0 = 不清理);進行中警示不清理。
- Telegram 通知走 HTTPS 驗證憑證;防火牆 SSL inspection 導致失敗時可於 config.json 設
  `telegram_skip_tls_verify: true`(僅影響 Telegram,訊息不含密碼)。

## 運維雜項

- 反向代理部署時,`start_vcod.cmd` 內 `TRUSTED_PROXY` 填代理 IP,uvicorn 才會
  信任 `X-Forwarded-For`(稽核來源 IP 正確)。
- 輪詢間隔 10–3600 秒(預設 30);去抖輪數 1–20(預設 3)→ 用量類警示反應時間 ≈ 間隔 × N。
  單座 vCenter 一輪抓取時間可於管理頁「最後輪詢」下方看到,大型環境請依此調整間隔。
- Guest 層規則需 VM 開機且 VMware Tools 執行中;`guest.disk` 的 `filesystemType` / 對 vmdk 的
  mappings 為 vSphere 7.0+ 才有,6.x 只有路徑與容量。VM Ready% 亦為 7.0+。
- 舊版 PyInstaller 打包不再提供;以 venv 部署為準。
