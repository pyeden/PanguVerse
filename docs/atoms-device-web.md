# 设备与 Web 自动化原子包（`web_browser` / `android_adb` / `ios_sim`）

> 契约来源：`tasks/device-web-atoms-spec.md` v7.0。
> 三个包都遵循同一条原则：**工具/设备不具备时显式失败，绝不编造成功**。

---

## 1. 三个包一览

| 包 | 原子数 | 依赖工具 | 能力声明 | 本机实测 |
| --- | --- | --- | --- | --- |
| `web_browser` | 5 | Chrome（`PANGUVERSE_CHROME`） | `browser, network` | ✅ Chrome 无头可用 |
| `android_adb` | 11 | `adb`（`PANGUVERSE_ADB`） | `device, process` | 工具 ✅ / 设备 ❌ |
| `ios_sim` | 8 | `xcrun simctl`（`PANGUVERSE_SIMCTL`） | `device, process` | 工具 ✅ / 模拟器 ❌ |

每个原子都带：

- `needs_tool` 属性（`chrome` / `adb` / `simctl`）；
- 标签 `tool:<name>`，因此 `/api/atoms?tag=tool:adb` 可直接筛选；
- 明确的 `effects`：`tap/input/launch/install/...` 为 `mutate`，探测/解析为 `read`/`pure`。

### 1.1 `web_browser`

| 原子 | 输入 | 输出 | 失败类 |
| --- | --- | --- | --- |
| `web_open` | `url`, `timeout?`, `wait_for?`, `interval?`, `virtual_time_ms?`, `max_chars?` | `status/html/text/title/final_url/attempts` | `network`, `missing_tool`, `timeout`, `parse` |
| `web_get_text` | `html` | `text/chars` | `parse` |
| `web_screenshot` | `url` 或 `html`, `width?`, `height?`, `name?` | `path/bytes/width/height` | `missing_tool`, `timeout`, `parse` |
| `web_extract_links` | `html`, `same_origin_only?`, `base_url?` | `links[]`, `count` | `parse` |
| `web_wait_for_text` | `url`, `text`, `timeout?`, `interval?` | `found/attempts/elapsed_ms` | `timeout`（**不可重试**） |

要点：

- DOM 来自 `chrome --headless=new --dump-dom --virtual-time-budget=<ms>`；
- `status`/`final_url` 由标准库轻量探测（只读 1 字节），不下载两遍正文；
- 正文提取**复用** `panguverse.packs.web_research.extract_text`，没有第二套正则；
- `web_open.html` 默认截断到 4000 字符；需要完整 DOM（大页面做链接分析）时把 `max_chars` 调大。

### 1.2 `android_adb`

| 原子 | 关键输入 | 输出 | 副作用 |
| --- | --- | --- | --- |
| `android_devices` | — | `devices[]`（serial/state/model） | read |
| `android_launch_app` | `package`, `activity?`, `serial?` | `ok/output` | **mutate** |
| `android_stop_app` | `package`, `serial?` | `ok` | **mutate** |
| `android_tap` | `x`, `y`, `serial?` | `ok` | **mutate** |
| `android_swipe` | `x1/y1/x2/y2`, `duration?` | `ok` | **mutate** |
| `android_input_text` | `text`, `serial?` | `ok` | **mutate** |
| `android_keyevent` | `key`, `serial?` | `ok` | **mutate** |
| `android_screencap` | `serial?`, `name?` | `path/bytes` | read + 本地写 |
| `android_ui_dump` | `serial?` | `xml/nodes[]` | read |
| `android_find_text` | `text`, `xml?`/`nodes?`, `match?` | `found/node` | pure |
| `android_wait_text` | `text`, `timeout?`, `interval?` | `found/attempts` | read |

设备选择规则：

- `android_devices` 是**探测原子**：没有设备返回 `devices=[]`，不报错；
- 单设备可省略 `serial`；多设备未指定 → `ambiguous_device`；
- 指定了 `serial` 但设备不在线/未授权 → `missing_device`；
- `uiautomator dump` 已知偶发抖动，**只有它会重试 1 次**；其余原子不自动重试。

### 1.3 `ios_sim`

| 原子 | 关键输入 | 输出 | 副作用 |
| --- | --- | --- | --- |
| `ios_sim_list` | — | `devices[]`（name/udid/state/runtime） | read |
| `ios_sim_boot` | `udid` | `ok/state` | **mutate** |
| `ios_sim_shutdown` | `udid` | `ok/state` | **mutate** |
| `ios_sim_install` | `udid`, `app_path` | `ok` | **mutate** |
| `ios_sim_launch` | `udid`, `bundle_id` | `pid` | **mutate** |
| `ios_sim_terminate` | `udid`, `bundle_id` | `ok` | **mutate** |
| `ios_sim_openurl` | `udid`, `url` | `ok` | **mutate** |
| `ios_sim_screenshot` | `udid`, `name?` | `path/bytes` | read + 本地写 |

`ios_sim_list` 是探测原子：没有可用模拟器返回 `devices=[]`，不报错。

---

## 2. 失败词汇（三类必须分清，且都不可重试）

| `failure_kind` | 触发条件 | `failure_class` | 用户可见文案示例 |
| --- | --- | --- | --- |
| `missing_tool` | 环境变量指向的/默认的工具不存在或不可执行 | `missing_resource` | `未找到 adb：PANGUVERSE_ADB=... 不存在或不可执行` |
| `missing_device` | 无设备 / 无模拟器 / 指定 serial 不在线 | `missing_resource` | `未连接设备：adb devices 没有处于 device 状态的安卓设备` |
| `ambiguous_device` | 多台设备但未指定 `serial` | `invalid_input` | `检测到多台安卓设备（SERIAL-A, SERIAL-B），请用 serial 明确指定` |
| `device_command_failed` | adb/simctl 非零退出或输出 Error | `internal` | 带截断后的 stderr |
| `timeout` | 命令超时（进程组 SIGKILL） | `transient` | 带命令与超时秒数 |
| `parse` | DOM/XML/JSON 无法解析 | `invalid_input` | 带输出样本 |

专用语义类把这些映射到引擎可消费的 `FailureClass`，且：

- `AndroidSemantics.is_retryable()` / `IosSimSemantics.is_retryable()` **永远返回 False**；
- `WebBrowserSemantics` 只有 `network` 可重试；`web_wait_for_text` 的 `timeout` 明确不可重试。

---

## 3. 工具路径覆盖

```bash
export PANGUVERSE_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
export PANGUVERSE_ADB="$HOME/Library/Android/sdk/platform-tools/adb"
export PANGUVERSE_SIMCTL="$(xcrun -f simctl)"   # 或 /usr/bin/xcrun（会自动补 simctl）
```

语义是**覆盖即锁定**：变量一旦设置，就不会回退到默认路径。因此把变量指向不存在的
文件会得到 `missing_tool`，而不是悄悄用另一个工具。

进程执行统一：

- `start_new_session=True`（独立进程组）；
- 超时后 `os.killpg(..., SIGKILL)`，父进程再回收；
- stdout/stderr 截断到 4000 字符（`clip_output`）；
- 截图等二进制走 `exec-out`/文件写入，不经文本截断。

---

## 4. 产物

统一落在工作区：

```
<workspace>/.panguverse/artifacts/web/       # web_screenshot
<workspace>/.panguverse/artifacts/android/   # android_screencap
<workspace>/.panguverse/artifacts/ios/       # ios_sim_screenshot
```

- 目录权限 `0700`，文件权限 `0600`；
- 返回的 `path` 相对工作区根（`.panguverse`），例如 `artifacts/web/example.png`；
- 文件名可用 `name` 指定，缺省带时间戳 + pid，避免并发覆盖。

---

## 5. 与规划器/校验的接缝

- 三个包加入 `panguverse.packs` 后，`/api/packs`、`/api/atoms` 自动可见；
- 每个包至少提供一条意图模板：
  - `web_browser_content` —— 无头浏览器打开网页并取正文；
  - `android_launch_screenshot` —— 安卓启动应用并截图；
  - `ios_simulator_list` —— 列出 iOS 模拟器；
- 原子上的 `needs_tool` / `tool:<name>` 标签可用于能力门禁：
  检查工具存在性后可给出 `missing_tool` 警告；检查 `android_devices` / `ios_sim_list`
  返回值可给出 `missing_device` 警告（不阻断保存，阻断运行）；
- `ios_sim` 包的 `capability_gaps` 属性（以及 `IOS_CAPABILITY_GAPS` 常量）声明了
  iOS 元素级交互缺口。

> 注意：`webui/**` 的 validate 汇总不在本变更范围内。本仓库的 `planner_gate` 目前
> 只认识 `web_research/local_fs/api_testing` 三个领域；iOS 意图的 `capability_gap`
> 已由包侧声明，但要在工作台自动返回 409，仍需在 `panguverse/web/planner_gate.py`
> 增加 iOS 领域词表（本轮按文件归属约束未改）。

---

## 6. 明确的能力缺口

iOS 上的**点击/输入/滑动不做**。它们需要 XCUITest 或 WebDriverAgent，属于另一套运行时。
规划器遇到「点击 iOS 元素」「在模拟器输入文本」这类意图时必须返回 `capability_gap`
（`ios_tap` / `ios_input_text` / `ios_swipe`），不得映射到安卓原子或伪造成功。

本轮也不做：Appium/WebDriverAgent 集成、真机云测、截图 diff。

---

## 7. 验收与自测

默认（无设备/无模拟器）也全绿：

```bash
cd /Users/futuredeng/PycharmProjects/PanguVerse
source .venv/bin/activate
pytest -q
ruff check panguverse tests
```

真实 Chrome 的 D1–D4（example.com）：

```bash
PANGUVERSE_LIVE=1 pytest -q tests/test_packs_device_web.py -k live_d
```

逐条对应：

| # | 测试 | 判定 |
| --- | --- | --- |
| D1 | `test_live_d1_web_open_example` | `status=200`、title 含 `Example`、text > 50 |
| D2 | `test_live_d2_web_extract_links_example` | links 非空，`internal` 判定一致 |
| D3 | `test_live_d3_web_screenshot_example` | 文件存在/非空/`0600`/位于 `artifacts/web/` |
| D4 | `test_live_d4_web_wait_for_text_example` / `_negative` | 正例 found=true；反例 timeout 且不可重试 |
| D5 | `test_d5_android_devices_without_device_returns_empty` | `devices=[]`，不报错 |
| D6 | `test_d6_android_tap_without_device_fails_missing_device` | `missing_device`，文案含「未连接设备」，只探测一次 |
| D7 | `test_d7_android_missing_tool` | `missing_tool`，不崩 |
| D8 | `test_d8_ios_list_without_simulator_returns_empty` | `devices=[]`，不报错 |
| D9 | `test_d9_ios_launch_without_simulator_fails_missing_device` | `missing_device`，文案明确 |

有设备/有模拟器的解析分支用 `tests/fixtures/fake_adb`、`fake_simctl`、`fake_chrome`
验证，不要求真机；覆盖 uiautomator XML、simctl JSON、截图产物与输出截断。
