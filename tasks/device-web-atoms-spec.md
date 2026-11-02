# 设备与 Web 自动化原子包 · 实现契约 v7.0

> 目标：给 PanguVerse 补上前端/移动端自动化的基础原语，让「意图 → 原子组合」能覆盖真实设备与网页。
> 本机现状（实测）：`adb 1.0.41` 已装但**无设备**；Xcode + `simctl` 可用但**无可用模拟器**；Chrome 无头可用（`--dump-dom` 已验证）；无 Playwright/Selenium。
> 因此三个包的验收必须区分「真机路径」与「工具/设备缺失时的显式失败」，**不允许静默跳过或假装成功**。

---

## 1. 三个领域包

| 包 | 依赖工具 | 本机可用性 | 能力声明 |
| --- | --- | --- | --- |
| `web_browser` | Chrome（本地二进制） | ✅ 可用 | `browser, network` |
| `android_adb` | `adb` + 已连接设备 | 工具 ✅ / 设备 ❌ | `device, process` |
| `ios_sim` | `xcrun simctl` + 模拟器 | 工具 ✅ / 模拟器 ❌ | `device, process` |

共同规则：
- 原子必须声明 `needs_tool`（`chrome|adb|simctl`）与 `capabilities`；工具缺失 → `failure_kind=missing_tool`，设备缺失 → `missing_device`，**两者都不可重试**。
- 所有命令执行必须有**超时**（默认 30s，可配）与输出截断（默认 4000 字符），避免日志爆炸。
- 全部只读/可控副作用明确：`mutate=true` 的原子（点击/输入/安装/启动）在发布前必须被风险确认（复用现有风险机制）。
- 不从网络安装任何东西；工具路径可覆盖（env：`PANGUVERSE_CHROME`、`PANGUVERSE_ADB`、`PANGUVERSE_SIMCTL`）。

## 2. `web_browser` 原子

| 原子 | 输入 | 输出 | 失败类 |
| --- | --- | --- | --- |
| `web_open` | `url`（必填）、`timeout?`、`wait_for?`（文本，轮询到出现） | `status`、`html`、`text`、`title`、`final_url` | `network`、`missing_tool`、`timeout`、`parse` |
| `web_get_text` | `html` | `text`、`chars` | `parse` |
| `web_screenshot` | `url` 或 `html`+`width/height?` | `path`、`bytes` | `missing_tool`、`timeout` |
| `web_extract_links` | `html`、`same_origin_only?` | `links[]`（href/text/internal） | `parse` |
| `web_wait_for_text` | `url`、`text`、`timeout?`、`interval?` | `found`、`attempts`、`elapsed_ms` | `timeout`（不可重试） |

实现要点：
- 用 Chrome 无头 `--headless=new --dump-dom --virtual-time-budget=<ms>` 获取渲染后 DOM；`--screenshot` 出图。
- 每个进程都要 `start_new_session` + 超时 kill，避免僵尸进程。
- 文本提取复用 `web_research.html_extract_text` 的规则（不要写第二份）。
- `web_screenshot` 产物落 `<workspace>/.panguverse/artifacts/web/`（0600 目录 0700），返回相对路径。

## 3. `android_adb` 原子

| 原子 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `android_devices` | — | `devices[]`（serial/state/model） | 无设备返回空数组，**不报错** |
| `android_launch_app` | `package`（必填）、`activity?`、`serial?` | `ok`、`output` | `am start -n` / `monkey -p` 兜底 |
| `android_stop_app` | `package`、`serial?` | `ok` | `am force-stop` |
| `android_tap` | `x`、`y`、`serial?` | `ok` | `input tap` |
| `android_swipe` | `x1,y1,x2,y2,duration?` | `ok` | `input swipe` |
| `android_input_text` | `text`、`serial?` | `ok` | `input text`（空格转义） |
| `android_keyevent` | `key`（如 `KEYCODE_BACK`）、`serial?` | `ok` | |
| `android_screencap` | `serial?`、`name?` | `path`、`bytes` | `exec-out screencap -p` → artifacts |
| `android_ui_dump` | `serial?` | `xml`、`nodes[]`（text/resource-id/bounds） | `uiautomator dump` + 解析 |
| `android_find_text` | `xml` 或 `text`、`match?` | `found`、`node` | 纯解析，无副作用 |
| `android_wait_text` | `text`、`timeout?`、`interval?` | `found`、`attempts` | 轮询 `ui_dump` |

规则：
- 单设备时 `serial` 可省略；多设备时必须显式指定，否则 `ambiguous_device`（不可重试）。
- 任何 adb 命令失败 → `device_command_failed`，带 stderr 截断；**超时 → `timeout`**。
- `uiautomator dump` 偶发失败重试 1 次（已知抖动），其余不自动重试。

## 4. `ios_sim` 原子

| 原子 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `ios_sim_list` | — | `devices[]`（name/udid/state/runtime） | 无模拟器返回空数组 |
| `ios_sim_boot` | `udid`（必填） | `ok`、`state` | 已 booted 直接返回 ok |
| `ios_sim_shutdown` | `udid` | `ok` | |
| `ios_sim_install` | `udid`、`app_path` | `ok` | `simctl install` |
| `ios_sim_launch` | `udid`、`bundle_id` | `pid` | `simctl launch` |
| `ios_sim_terminate` | `udid`、`bundle_id` | `ok` | |
| `ios_sim_openurl` | `udid`、`url` | `ok` | 深链/网页打开 |
| `ios_sim_screenshot` | `udid`、`name?` | `path`、`bytes` | `simctl io screenshot` |

**明确不做（本轮）**：iOS 上的点击/输入/滑动——需要 XCUITest 或 WebDriverAgent，属另一套运行时；
`ios_sim` 包必须声明能力缺口，规划器遇到「点击 iOS 元素」类意图要返回 `capability_gap`，而不是编一个原子出来。

## 5. 与规划器/校验的接缝

- 三个包加入 registry 后，`/api/packs`、`/api/atoms` 自动出现；意图模板要补最少三条示例（网页取内容、安卓启动并截图、iOS 模拟器列表）。
- **能力门禁**：意图涉及设备操作但对应工具/设备不可用时，`validate` 返回 `missing_tool` / `missing_device` 警告（不阻断保存，阻断运行）。
- 风险徽标：`android_tap`/`android_input_text`/`ios_sim_install` 等 `mutate=true`，发布前需确认。

## 6. 验收（D1–D10）

| # | 项 | 判定 |
| --- | --- | --- |
| D1 | `web_open` 真实可用 | 打开 `https://example.com` → `status=200`、`title` 含 Example、`text` 长度 >50 |
| D2 | `web_extract_links` | 同一页面 → `links` 非空且 `internal` 判定正确 |
| D3 | `web_screenshot` | 产出文件存在、非空、落在 artifacts 且 0600 |
| D4 | `web_wait_for_text` | 对含该文本的页面 → `found=true, attempts>=1`；不存在的文本 → `timeout` 且**不可重试** |
| D5 | `android_devices` 无设备 | 返回 `devices=[]`，**不报错** |
| D6 | 安卓原子无设备时 | `android_tap` → `missing_device`，错误文案含"未连接设备"，**不重试** |
| D7 | adb 缺失 | 把 `PANGUVERSE_ADB` 指向不存在路径 → `missing_tool`，不崩 |
| D8 | `ios_sim_list` 无模拟器 | 返回 `devices=[]`，不报错 |
| D9 | `ios_sim_launch` 无设备 | `missing_device` 或 `missing_simulator`，文案明确 |
| D10 | 回归 | `pytest` 全绿、`ruff` 0；既有 A17–A62/W1–W9 不回归；三包在 `/api/packs` 可见、`/api/atoms` 原子数正确 |

补充：设备/模拟器不可用时用**假工具脚本**（PATH shim 或 env 指向桩程序）验证「有设备」路径的解析逻辑（uiautomator XML 解析、simctl JSON 解析），不要求真机。

## 7. 文件归属

|coder|
| --- |
| `panguverse/packs/web_browser.py`、`android_adb.py`、`ios_sim.py` |
| `panguverse/packs/__init__.py`（注册） |
| `tests/test_packs_device_web.py`、`tests/fixtures/`（假工具桩、XML/JSON 样本） |
| `docs/atoms-device-web.md`（用法与限制） |

不得改：`core/engine/axe` 语义、`dependencies`（保持 `[]`）、`webui/**`、`static/**`。

## 8. 明确不做

- Appium/WebDriverAgent 集成（P2，需要 Node/Java 运行时）。
- 真机云测平台对接。
- iOS 元素级交互（见 §4）。
- 视觉回归（截图 diff）。
