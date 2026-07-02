# comfyui-doubao-seedance

> 在 ComfyUI 里生成视频 —— 豆包 **Seedance 2.0** 全系列（走 DMXAPI），**6 个功能**全覆盖。
> 4 个生成节点 + 1 个通用获取节点：生成节点提交任务拿 `task_id` → 获取节点轮询、下载、预览 MP4。

---

## 🔐 Security Disclosure (for reviewers & users)

This node pack makes **exactly three kinds of network calls**, all disclosed here and in the node tooltips. There is nothing else — no telemetry, no analytics, no remote code download / `eval`, no credential harvesting. The user's API key is sent **only** to the API endpoint the user configures.

1. **DMXAPI video-generation API** (`POST {base_url}/v1/responses`, default `https://www.dmxapi.cn`, user-configurable) — the core function. Generation requests (prompt + reference media + the user's own DMXAPI key) are submitted here, and task status is polled here.
2. **Anonymous temp-file upload to `litterbox.catbox.moe`** — used **only** by the optional `video_1..3` VIDEO inputs of the "参考生成视频 / Reference-to-Video" node. Reason: the DMXAPI backend accepts reference *videos* only as public URLs (no upload endpoint, no base64), so to let users connect a local *Load Video* node, the video is uploaded to litterbox (an anonymous temporary file host; links auto-expire after 72 hours) and the resulting URL is passed to the API. This is functionally the same pattern as the official ByteDance partner node (which uploads to comfy.org storage instead — an endpoint this third-party-API pack cannot use).
   - The privacy implication is **prominently warned** in this README and in the `video_1` socket tooltip (*"private videos: do NOT use this input"*).
   - A **no-upload alternative always exists**: the `video_url_1..3` string inputs take a public URL / `asset://` ID and never touch any third-party host. Images and audio are never uploaded anywhere except to DMXAPI itself (inline base64 in the API request).
3. **Result download** — the finished MP4 is downloaded from the URL returned by DMXAPI into the local ComfyUI `output/doubao/` folder.

---

## 一、这个插件能做什么（6 大功能）

| # | 功能 | 用哪个节点 | 输入 |
|---|---|---|---|
| 1 | **文生视频** | 🎬豆包 Seedance 文生视频 | 提示词 |
| 2 | **首帧生视频**（图生视频） | 🎬豆包 Seedance 首帧生视频 | 提示词 + 1 张首帧图 |
| 3 | **首尾帧生视频** | 🎬豆包 Seedance 首尾帧 | 提示词 + 首帧图 + 尾帧图 |
| 4-6 | **视频编辑 / 视频延长 / 多模态参考** | 🎬豆包 Seedance 参考生成视频 | 提示词 + 动态参考素材（图/视频/音频/素材ID） |

后三个功能合并成**一个动态节点**「参考生成视频」——像官方节点那样，接口随连线**自动增长**（详见「六之三」）。

所有功能都走"**生成节点 → 获取节点**"两步，获取节点 6 功能通用：

```
 (按功能接图/填URL)                                                    
 [加载图像]──IMAGE──┐                                                   
                   ▼                                                   
┌──────────────────────────┐         ┌──────────────────────────┐
│  🎬豆包 Seedance <某功能>  │ task_id │  🎬豆包 Seedance 获取      │
│ （提示词 + 媒体输入）      ├────────▶│  （轮询、下载、预览）      │
│                          │get_model│                          │
│  输出: task_id           ├────────▶│  输出: video / video_url  │
│        get_model         │         │                          │
└──────────────────────────┘         └──────────────────────────┘
```

> 本插件是「基础版」：覆盖 6 功能核心参数，不含联网搜索（web_search）、不含尾帧 PNG 返回（return_last_frame）。
> **重要限制**：参考**图片**可用 URL / 本地文件（IMAGE 接口）/ 素材ID；参考**音频**可用本地文件（AUDIO 接口，自动转 base64）/ URL / 素材ID；参考**视频**可接「加载视频」（VIDEO 接口，节点自动匿名上传到临时文件床换 URL，**有隐私风险**）或直接填 URL / `asset://` 素材ID（`video_url`，不经上传）。DMXAPI 视频端本身只收 URL/素材ID、不收本地文件——本地视频靠插件上传中转实现。

---

## 二、安装

三种方式任选其一：

- **方式一（推荐）：ComfyUI 管理器安装**——打开「自定义节点管理器」，搜索 `comfyui-doubao-seedance`（或"豆包 Seedance"）→ 安装。
- **方式二：git 安装**——在 `custom_nodes\` 目录下执行 `git clone https://github.com/YV919/comfyui.git`（clone 出的文件夹名是 `comfyui`，不影响加载）。
- **方式三：手动**——下载本仓库，把整个文件夹放进 ComfyUI 的 `custom_nodes\` 目录。

装好后：

1. **彻底退出 ComfyUI Desktop 进程，再重新打开**（刷新网页没用——代码是启动时加载进内存的）。
2. 画布空白处双击搜 `豆包`（或右键添加节点 → **DMXAPI → 豆包视频模型**），能看到 5 个节点（🎬豆包 Seedance 文生视频 / 首帧生视频 / 首尾帧 / 参考生成视频 / 获取）= 安装成功。

依赖：用到 `requests` / `Pillow` / `numpy` / `av`（音频编码用，均为 ComfyUI 环境自带），无需额外安装。**需要较新版本的 ComfyUI**（本插件使用 `comfy_api.latest` V3 节点 API，过旧版本会加载失败——ComfyUI Desktop 保持更新即可）。

> ⚠️ **从旧版本升级的用户**：模型输入移到最上方 + 界面汉化后，旧存档工作流会**参数错位**（比如提示词被当成模型名），请重新拖入本插件 `example_workflows\` 里的新版工作流。

---

## 三、准备

- **DMXAPI Key**：登录 [DMXAPI 后台](https://www.dmxapi.cn/) → 令牌管理 → 复制 `sk-xxxxxx`。
- **余额**：视频模型按次计费，账户要有余额。**两个节点都要填同一个 key。**

---

## 四、最快上手：导入现成工作流

插件自带 4 个连好线的示例工作流（在插件目录的 `example_workflows\` 里，ComfyUI 的「工作流模板」浏览器里也能直接找到），按需求选一个**直接拖进 ComfyUI 画布**：

| 文件 | 功能 |
|---|---|
| `豆包Seedance工作流.json` | **①文生视频**（生成 → 获取） |
| `豆包首帧生视频工作流.json` | **②首帧生视频**（加载图像 → 生成 → 获取） |
| `豆包首尾帧生视频工作流.json` | **③首尾帧**（2×加载图像 → 首尾帧 → 获取） |
| `豆包参考生成视频工作流.json` | **④⑤⑥ 视频编辑/延长/多模态**（加载图像 → 参考生成视频 → 获取） |

拖进去节点自动出现并连好线 → 跳到「六、填参数」。

> ⚠️ 凡是含「**加载图像（Load Image）**」的工作流：导入后要在该节点上点 **choose file to upload / 选择文件**，换成你自己的图片（默认的 `example.png` 只是占位符，不换会报「example.png not found」）。
> ⚠️ **参考生成视频**工作流：要参考视频有两种给法——直接把「**加载视频（Load Video）**」节点接到 `video_1`（VIDEO 接口，节点会自动上传换 URL，**有隐私风险**）；或在 `video_url_1` 文本框填公网 URL / `asset://` 素材ID（不经上传）。详见「六之三」。

---

## 五、从零手动搭建（学习用）

1. 双击画布空白处 → 搜 `豆包 Seedance` → 按需选一个**生成节点**（如 `文生视频` 或 `首帧生视频`）放到画布。
2. 再双击 → 搜 `豆包 Seedance 获取` → 放到右边。
3. 连两根线（关键）：

| 从（生成节点输出） | 拖到（获取节点输入） |
|---|---|
| `task_id` | `task_id` |
| `get_model` | `get_model` |

> 拖线：鼠标按住输出点的小圆点，拉到目标输入点的小圆点，松手。
> 连对后获取节点的 `task_id` / `get_model` 会从输入框变成「已连线」状态。
> 首帧/首尾帧/视频编辑/多模态等功能，还要按各自节点接上图片（加载图像）或填视频 URL，详见「六之二」「六之三」。

---

## 六、填参数

### 文生视频节点（🎬豆包 Seedance 文生视频）

> 节点里的输入名都已汉化；括号里是对应的英文/接口字段，方便对照。
> **「模型」在节点最上方**，默认 `doubao-seedance-2-0-260128`，一般不用改。

| 参数（节点显示） | 填什么 | 说明 |
|---|---|---|
| **模型** | 默认即可 | 默认 `doubao-seedance-2-0-260128`；想用别的模型名才改 |
| **提示词** | 中文描述 | 例：`写实风格，蓝天下大片白色雏菊花田，镜头拉近定格在一朵雏菊特写，花瓣有露珠` |
| **密钥(api_key)** | 你的 `sk-xxxxx` | DMXAPI 令牌 |
| **接口地址** | `https://www.dmxapi.cn` | 默认即可，不用改 |
| **宽高比** | `16:9` | 横屏 16:9 / 竖屏 9:16 / 方形 1:1 / 4:3 / 3:4 / 超宽 21:9 / `adaptive`（智能选） |
| **分辨率** | `480p` | 480p / 720p / 1080p。**先用 480p 省钱测试** |
| **时长(秒)** | `5` | Seedance 2.0 支持 **4–15** |
| **生成音频** | `true` | 是否生成与画面同步的音频（人声/音效/BGM，单声道） |
| **种子** | `-1` | `-1` = 随机；固定值可复现类似结果。「生成后控制」设 `randomize` 每次出不同视频 |
| **水印** | `false` | 是否含水印 |

> **首帧生视频**（给一张首帧图）请用单独的「🎬豆包 Seedance 首帧生视频」节点，参数同上、另多「首帧图片」/「首帧图片链接」两项，详见「六之二」。其它功能（首尾帧/参考生成视频）见「六之三」。

### 获取节点（🎬豆包 Seedance 获取）

| 参数（节点显示） | 填什么 | 说明 |
|---|---|---|
| **任务ID** / **检索模型** | 从生成节点连线 | 生成节点的两个输出连过来即可，不用手填 |
| **密钥(api_key)** | 同上 `sk-xxxxx` | **两个节点都要填 key** |
| **接口地址** | `https://www.dmxapi.cn` | 默认 |
| **最大等待(秒)** | `300` | 最多等待秒数。视频久就调大（如 600） |
| **轮询间隔(秒)** | `6` | 每隔几秒查一次进度 |
| **保存名前缀** | `doubao/seedance` | 保存的子目录/文件名前缀 |
| 输出 **视频** / **视频链接** | —— | 视频可直接预览；视频链接是 CDN 地址（24 小时失效） |

---

## 六之二、首帧生视频（图生视频）

用「🎬豆包 Seedance 首帧生视频」节点，给一张**首帧图片**，模型会以它为视频第一帧、按提示词让画面动起来。两种给图方式：

- **方式 A（推荐）接 IMAGE**：加一个 ComfyUI 自带「**加载图像（Load Image）**」节点，把它的 `IMAGE` 输出连到节点的 `first_frame` 输入。最快办法是直接拖入 `豆包首帧生视频工作流.json`。
- **方式 B 填链接/路径**：不接 IMAGE，在节点的 `image_url` 文本框填**网络图片链接**（`https://...`）或**本地图片路径**（如 `C:/图片/a.png`）或素材ID（`asset://...`）或 data URL。

> 优先级：连了 `first_frame`（IMAGE）就用它，`image_url` 会被忽略；**两个都不给会报错**（纯文生视频请改用「🎬豆包 Seedance 文生视频」节点）。

**首帧场景建议**：
- `ratio` 选 **`adaptive`**（按首帧图片比例自动选最接近的画幅，避免裁剪变形）。
- 提示词照常写——描述你想要的动作/运镜（例：`图中小狗对着镜头说"茄子"，360度环绕运镜`）。

**首帧图片要求**（豆包接口限制）：
- 格式：jpeg / png / webp / bmp / tiff / gif
- 宽高比：0.4 ～ 2.5 之间
- 边长：300 ～ 6000 px
- 大小：≤ 30 MB

---

## 六之三、其它功能（③ 首尾帧 / ④⑤⑥ 参考生成视频）

这些节点**顶层参数都和生成节点一样**（模型 / 提示词 / 密钥 / 接口地址 / 宽高比 / 分辨率 / 时长 / 生成音频 / 种子 / 水印，「模型」都在最上方），只是多了各自的媒体输入。**都接同一个「🎬豆包 Seedance 获取」节点**取结果。

> ③ 首尾帧仍是独立节点；④视频编辑/⑤视频延长/⑥多模态参考已**合并成一个「参考生成视频」节点**（动态 socket，见下）。

### ③ 首尾帧生视频 —— `🎬豆包 Seedance 首尾帧`
- 输入：`first_frame`(IMAGE) / `first_frame_url`，`last_frame`(IMAGE) / `last_frame_url`。**首帧、尾帧都必须给**（各自 IMAGE 优先，否则用 URL）。
- 模型以首帧为开头、尾帧为结尾，中间自动补全过渡。推荐 `ratio=adaptive`（以首帧比例为准）。
- 现成工作流：`豆包首尾帧生视频工作流.json`（两个加载图像分别接首帧、尾帧）。

### ④⑤⑥ 参考生成视频 —— `🎬豆包 Seedance 参考生成视频`

视频编辑 / 视频延长 / 多模态参考**三合一**节点，做法仿官方「ByteDance Seedance 2.0 参考生成视频」——接口**动态增长**：每种类型先显示一个空 socket，连上后**自动冒出下一个**。

输入（4 组动态 socket）：
- **图 `image_1..9`**（IMAGE 接口）：接「加载图像 Load Image」节点。最多 9 张。
- **音频 `audio_1..3`**（AUDIO 接口）：接「加载音频 Load Audio」节点，本地音频自动转 **wav** base64（官方仅支持 wav/mp3）。最多 3 段，单段时长须在 **2~15 秒**内、合计 ≤15 秒。
- **视频**——两种给法（合计 ≤3 段）：
  - **`video_1..3`（真 VIDEO 接口）**：直接接「**加载视频（Load Video）**」节点。节点内部会把视频**匿名上传到临时文件床（litterbox.catbox.moe）**换取公网 URL 再发豆包。
    > ⚠️ **隐私**：视频会上传到**第三方公开文件床**，链接 72 小时内公开可访问（之后自动删）。**私密视频请勿用这个，改用下面的「视频链接」**。免费服务大视频较慢、偶发失败会报错，重试即可。
  - **`video_url_1..3`（视频链接）**：直接填**公网 URL 或 `asset://` 素材ID**，**不经第三方上传、隐私安全**——手头已有链接/素材ID 时推荐用这个。也可把「获取」节点的 `video_url` 输出接进来做视频延长。
- **素材 `asset_1..9`**（**STRING socket**）：填素材ID（`asset://...`，裸 ID 会自动补前缀）。**本期仅支持图片类素材**；视频/音频素材ID暂不支持（DMXAPI 无法判别素材类型，会被当图提交而可能失败）。`asset://` 能否被 DMXAPI 接受需真机确认。

规则：
- **至少给 1 张图 / 1 段视频 / 1 个素材**（不能只给音频）。
- 上限：图+素材 ≤9、视频（VIDEO 接口 + 视频链接合计）≤3、音频 ≤3。
- 提示词可用 `[图1][视频1]…` 引用素材（顺序：图→素材→视频→音频）。
- 这一个节点同时覆盖：**视频编辑**（给图/视频做参考改写）、**视频延长**（给视频续写，接「加载视频」或把「获取」的 `video_url` 填进 `video_url_1`）、**多模态参考**（图+视频+音频混合参考）。
- **若豆包报"无法拉取视频 / 生成失败"**：可能是免费文件床域名被目标侧拦截，改用 `video_url_1..3` 填一个你自己的公网视频链接 / 素材ID。
- 本节点**没有** `自动降采样 / auto_upscale`——那是字节官方节点参数，DMXAPI 接口里不存在。
- `generate_audio` 在该功能下文档标注"当前不生效"（视频始终含音轨），保留只为与其它节点一致。
- 音频 base64 默认 **WAV**（官方文档规定音频仅支持 wav/mp3，早期版本用 FLAC 会被豆包报"格式错误"）；如需更小体积可把 `AUDIO_B64_FORMAT` 改成 `"mp3"`。
- 现成工作流：`豆包参考生成视频工作流.json`。

---

## 七、运行 & 看结果

1. 点右上角 **▶ 运行**。
2. 看 ComfyUI **控制台**，会依次打印：
   ```
   [豆包Seedance] 提交生成请求 → doubao-seedance-2-0-260128
   [豆包Seedance] task_id = cgt-2026xxxx-xxxxx，检索模型 = seedance-2-0-get
   [豆包Seedance] 第 1 次查询：状态 …，等待 6s …
   [豆包Seedance] 生成完成，开始下载：https://…/xxx.mp4
   [豆包Seedance] 已保存到 output/doubao/...      ← 成功！
   ```
3. **获取节点**会显示视频预览，可直接播放。
4. 视频文件保存在 ComfyUI 输出目录下 `output\doubao\` 里的 `.mp4`。

---

## 八、常见报错速查

| 现象 | 原因 | 解决 |
|---|---|---|
| `请填写 DMXAPI 的 api_key` | 某个节点没填 key | 两个节点都填上 key |
| `首帧图片路径不存在：...` | image_url 填了本地路径但文件不在 | 检查路径；或改接「加载图像」节点用 IMAGE 输入 |
| `参考视频只支持 URL 或素材ID…不支持本地文件` | **video_url** 文本框填了本地路径 | 该文本框只收 URL/`asset://`；本地视频请接 `video_1`（VIDEO 接口，会自动上传） |
| `参考生成视频至少需要 1 张图 / 1 段视频 / 1 个素材ID` | 参考生成视频节点媒体全空 | 至少接 1 张图（image_1）/ 1 段视频（接 video_1 或填 video_url_1）/ 1 个素材ID（asset_1） |
| `参考图 + 素材最多 9` / `参考视频最多 3` / `参考音频最多 3` | 超过数量上限 | 减少对应类型的接入数量 |
| 豆包报音频"格式错误" | 旧版插件把音频转成 FLAC（官方仅收 wav/mp3），已改为 WAV | 更新插件后**彻底重启** ComfyUI Desktop；若仍报错，检查音频时长是否在 2~15 秒内（官方限制：单段 [2,15]s、≤3 段、总时长 ≤15s、单段 ≤15MB） |
| 豆包报"无法拉取视频 / 生成失败" | 免费文件床链接被目标侧拦截 | 改用 `video_url_1` 填你自己的公网视频链接 / 素材ID |
| `第 N 次下载未就绪（HTTP 404），等待…` | 视频刚生成，CDN 还没就绪 | **正常现象**，会自动重试，等出现「已保存到」即可 |
| `视频已生成但下载失败` | 重试多次仍取不到 | 重新运行一次；或把 `video_url` 复制到浏览器看是否能打开 |
| `等待超时` | 生成太慢超过 max_wait | 把获取节点 `max_wait_seconds` 调大到 600 |
| `未能从响应中解析出任务 id` | 接口返回结构变化 | 看控制台打印的原始响应，核对 task_id 所在字段 |
| 控制台 `task_id =` 不是 `cgt-` 开头 | 可能取到了 envelope id | 豆包真实 task_id 应以 `cgt-` 开头；若不是，把原始响应发来核对 |
| 改了插件代码后没生效 | ComfyUI 用的是内存里的旧代码 | **彻底退出 ComfyUI Desktop 进程再重开**（刷新网页没用） |

---

## 九、注意

- **Bearer 说明**：原始示例 `文生视频.py` 的 `Authorization` 缺少 `Bearer ` 前缀（与 `获取结果,.py` 不一致），本插件内已统一为 `Bearer`（与获取端及 DMXAPI 文档一致），无需你操心。
- **计费**：视频按次计费，建议先用 `480p` + 短时长测试，确认效果再调高。

---

## 十、安全提醒

- API Key 直接填在节点里。**不要把含 key 的工作流 json 分享/截图给别人。**
- 一旦 key 泄露，去 DMXAPI 后台删掉重建一个。

---

_本插件参数对齐豆包 Seedance 2.0 官方文生视频接口；轮询/下载重试/预览逻辑沿用已验证的 comfyui-dmxapi-video 实现。_
