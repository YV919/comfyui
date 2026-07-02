# -*- coding: utf-8 -*-
"""
豆包 Seedance 2.0 视频生成自定义节点（ComfyUI V3）

覆盖 Seedance 2.0 全系列 6 个功能，共 4 个生成节点 + 1 个通用获取节点：
  1. 🎬豆包 Seedance 文生视频     —— 文生视频（仅提示词）
  2. 🎬豆包 Seedance 首帧生视频   —— 首帧生视频（提示词 + 1 张首帧图：接 IMAGE 或填 image_url）
  3. 🎬豆包 Seedance 首尾帧       —— 首尾帧生视频（提示词 + 首帧图 + 尾帧图）
  4. 🎬豆包 Seedance 参考生成视频 —— 视频编辑 / 视频延长 / 多模态参考（合一）：动态增长 socket
       图 image_1..9（IMAGE）/ 视频 video_1..3（VIDEO 接口，接加载视频自动上传换 URL）/
       视频链接 video_url_1..3（STRING URL/素材ID）/ 音频 audio_1..3（AUDIO）/ 素材 asset_1..9（STRING）
  · 🎬豆包 Seedance 获取         —— 通用：轮询任务，下载 mp4，输出可预览视频（共用）

豆包 Seedance 走 DMXAPI 的私有异步接口：
  POST https://www.dmxapi.cn/v1/responses
  生成时返回任务 id（形如 cgt-xxx）；再用 "seedance-2-0-get" 模型轮询，
  从 output[0].content[0].text 内嵌 JSON 的 content.video_url 里拿视频地址。
  6 个功能走同一接口、同一组顶层参数、同一套获取流程，唯一差异是 input 数组的组成
  （文本 + 哪些媒体对象 + 各自的 role）。

媒体来源限制（官方文档）：图片支持 URL / base64 / 素材ID(asset://)；音频支持 URL / base64
  / 素材ID（故可接 ComfyUI AUDIO 接口，本地音频自动转 base64——注意音频格式官方仅收 wav/mp3，
  故 base64 用 wav 编码）；视频 DMXAPI 端【仅】收 URL / 素材ID，
  不收本地文件 / base64——本插件的 VIDEO 接口通过匿名上传到临时文件床（litterbox）换取公网 URL
  来支持「加载视频」（有隐私风险，见 README）；也可用 video_url 直接填 URL/素材ID（不经上传）。

本插件为「基础版」：覆盖 6 功能核心参数，不含联网搜索 web_search、
不含尾帧 PNG 返回（return_last_frame）等进阶功能。

辅助函数（轮询/下载重试/响应解析）沿用已验证可用的 comfyui-dmxapi-video 实现，
本插件自包含，不跨插件 import。
"""

import os
import json
import time
import random
import base64
import re
from io import BytesIO          # 仅取 BytesIO，避免 `import io` 覆盖 comfy_api 的 io
from urllib.parse import urlsplit, urlunsplit, unquote

import numpy as np
from PIL import Image
import av
import requests
import folder_paths
from comfy_api.latest import io, ui, InputImpl, ComfyExtension, Types

DEFAULT_BASE = "https://www.dmxapi.cn"

# 生成模型 → 检索模型（豆包 Seedance 2.0）
GENERATE_MODEL = "doubao-seedance-2-0-260128"
GET_MODEL = "seedance-2-0-get"

# ───────────────────────────── 辅助函数 ─────────────────────────────


def _headers(api_key):
    """统一用 Bearer 形式（豆包获取端示例即用 Bearer；裸 token 也兼容）。

    注：原始 文生视频.py 样例的 Authorization 缺 "Bearer " 前缀，与获取端样例
    （获取结果,.py）及 DMXAPI 文档不一致；此处统一为 Bearer。
    """
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key.strip()}",
    }


def _tensor_to_data_url(image_tensor):
    """ComfyUI IMAGE 张量 [B,H,W,C] float 0-1 → 首帧 PNG 的 base64 data URL。

    取批次第 0 帧作为首帧；惯用法同 comfyui_llm_party/llm.py。
    """
    if image_tensor.shape[0] > 1:
        print(f"[豆包Seedance] 警告：IMAGE 输入含 {image_tensor.shape[0]} 帧，仅取第 0 帧作为首帧")
    frame = image_tensor[0]
    arr = 255.0 * frame.cpu().numpy()
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _resolve_image_url(source):
    """文本图片来源 → 可直接用的 url。

    http(s):// 或 data: 原样返回；否则按本地文件路径读取并转 base64 data URL。
    """
    s = source.strip()
    if s.startswith(("http://", "https://", "data:")):
        return s
    if not os.path.isfile(s):
        raise RuntimeError(f"[豆包Seedance] 首帧图片路径不存在：{s}")
    ext = os.path.splitext(s)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    with open(s, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/{ext};base64,{b64}"


def _build_seedance_payload(model, prompt, ratio, resolution, duration, audio, seed,
                            watermark, image_data_url=None):
    """[已弃用] 文生/首帧旧载荷构造器。

    节点拆分后文生/首帧改用 `_seedance_top_level` + `_text_item`/`_image_item`
    （与其它功能节点风格统一）。此函数保留仅为向后兼容外部引用，现无内部调用方。
    """
    content = [_text_item(prompt)]
    if image_data_url:
        content.append(_image_item(image_data_url, "first_frame"))
    return _seedance_top_level(model, content, ratio, resolution, duration, audio, seed, watermark)


# ── 多功能（首尾帧/视频编辑/视频延长/多模态参考）共享构造器 ──
# 6 个功能走同一接口，差异仅在 input 数组组成：文本 + 媒体对象（各带 role）。


def _text_item(text):
    return {"type": "text", "text": text}


def _image_item(url, role):
    return {"type": "image_url", "image_url": {"url": url}, "role": role}


def _video_item(url, role):
    return {"type": "video_url", "video_url": {"url": url}, "role": role}


def _audio_item(url, role):
    return {"type": "audio_url", "audio_url": {"url": url}, "role": role}


def _resolve_media_url(source, allow_base64, kind):
    """媒体来源字符串 → 可直接提交的 url。

    http(s):// 或 asset:// 原样返回；data: 仅在 allow_base64 时放行；
    本地路径：allow_base64 则读文件转 base64，否则报错。
    视频/音频 allow_base64=False（接口仅支持 URL / 素材ID，不支持本地 / base64 上传）。
    """
    s = source.strip()
    if s.startswith(("http://", "https://", "asset://")):
        return s
    if s.startswith("data:"):
        if allow_base64:
            return s
        raise RuntimeError(f"[豆包Seedance] {kind}只支持 URL 或素材ID(asset://)，不支持 base64：{s[:40]}…")
    if not allow_base64:
        raise RuntimeError(f"[豆包Seedance] {kind}只支持 URL 或素材ID(asset://)，不支持本地文件：{s}")
    if not os.path.isfile(s):
        raise RuntimeError(f"[豆包Seedance] {kind}路径不存在：{s}")
    return _resolve_image_url(s)     # 复用现有「本地图片 → base64 data URL」逻辑


# 参考视频上传：DMXAPI 视频参考只收公网 URL/素材ID、无上传通道，故本地视频先匿名上传
# 到临时文件床（litterbox，发完即弃）换取公网直链再发送。端点/过期时间做成常量便于日后调整。
LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
LITTERBOX_EXPIRE = "72h"   # 1h/12h/24h/72h；选最长以防豆包任务较慢


def _upload_video_to_litterbox(video):
    """ComfyUI VIDEO(VideoInput) → 匿名上传临时文件床 → 返回公网直链 URL。

    ⚠️ 视频会上传到第三方公开文件床（litterbox.catbox.moe），链接短期公开可访问、
       72h 后自动删除；免费服务可能限速/不稳。私密视频请勿使用（改填 video_url）。
    """
    buf = BytesIO()
    video.save_to(buf, format=Types.VideoContainer.MP4, codec=Types.VideoCodec.H264)
    data = buf.getvalue()
    print(f"[豆包Seedance] 上传参考视频到临时文件床（{len(data) // 1024} KB）…")
    resp = requests.post(
        LITTERBOX_API,
        data={"reqtype": "fileupload", "time": LITTERBOX_EXPIRE},
        files={"fileToUpload": ("ref.mp4", data, "video/mp4")},
        timeout=300,
    )
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"[豆包Seedance] 视频上传失败，文件床返回：{url[:200]}")
    print(f"[豆包Seedance] 参考视频已上传：{url}")
    return url


def _video_source_to_url(src):
    """参考视频来源 → 可提交给豆包的 url。

    - VideoInput 对象（接了「加载视频」）→ 匿名上传换 URL
    - 字符串（http/https/asset://）→ 原样返回（不经第三方上传）
    """
    if isinstance(src, str):
        return _resolve_media_url(src, False, "参考视频")   # 仅 URL/素材ID
    if hasattr(src, "save_to"):                             # VideoInput 对象
        return _upload_video_to_litterbox(src)
    raise RuntimeError(f"[豆包Seedance] 无法识别的视频输入类型：{type(src)}")


def _socket_index(name):
    """把 "image_10" 排在 "image_2" 之后（按 socket 名尾部数字数值序）。"""
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


# 音频 base64 编码格式：官方文档规定音频仅支持 wav/mp3（见示例"6.多模态参考生视频"注释，
# flac 会被豆包报"格式错误"）。默认 wav（无损、任意采样率都能编）；如需更小体积可改 "mp3"
#（mp3 仅支持 8k~48k 固定采样率档位，非常规采样率会编码失败）。
AUDIO_B64_FORMAT = "wav"
_AUDIO_CODEC = {"flac": "flac", "wav": "pcm_s16le", "mp3": "libmp3lame"}
_AUDIO_MIME = {"flac": "audio/flac", "wav": "audio/wav", "mp3": "audio/mpeg"}


def _audio_input_to_data_url(audio):
    """ComfyUI AUDIO（{"waveform":[B,C,T], "sample_rate":int}）→ base64 data URL。

    惯用法同 comfy_api/latest/_ui.py 的 AudioSaveHelper（PyAV 编码）。取批次第 0 条。
    """
    wf = audio["waveform"]
    if wf.shape[0] > 1:
        print(f"[豆包Seedance] 警告：AUDIO 输入含 {wf.shape[0]} 条，仅取第 0 条")
    wf0 = wf[0].cpu()                          # [C, T]
    sr = int(audio["sample_rate"])
    layout = "mono" if wf0.shape[0] == 1 else "stereo"
    fmt = AUDIO_B64_FORMAT
    buf = BytesIO()
    with av.open(buf, mode="w", format=fmt) as oc:
        st = oc.add_stream(_AUDIO_CODEC[fmt], rate=sr, layout=layout)
        fr = av.AudioFrame.from_ndarray(
            wf0.movedim(0, 1).reshape(1, -1).float().numpy(), format="flt", layout=layout
        )
        fr.sample_rate = sr
        fr.pts = 0
        oc.mux(st.encode(fr))
        oc.mux(st.encode(None))
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:{_AUDIO_MIME[fmt]};base64,{b64}"


def _seedance_top_level(model, content, ratio, resolution, duration, audio, seed, watermark):
    """组装 6 功能通用的顶层载荷（content 即各功能拼好的 input 数组）。"""
    return {
        "model": model,
        "input": content,
        "ratio": ratio,
        "resolution": resolution,
        "duration": duration,
        "generate_audio": bool(audio),
        "seed": seed,
        "watermark": bool(watermark),
    }


def _submit_task(model, base_url, api_key, payload):
    """POST 提交生成请求 + 解析 task_id + 打印日志；6 个生成节点共用。

    model 仅用于提交前的日志行。
    """
    url = base_url.strip().rstrip("/") + "/v1/responses"
    print(f"[豆包Seedance] 提交生成请求 → {model}")
    resp = requests.post(url, headers=_headers(api_key), json=payload, timeout=120)
    resp.raise_for_status()
    resp_json = resp.json()
    task_id = _extract_task_id(resp_json)
    if not task_id:
        raise RuntimeError(
            "未能从响应中解析出任务 id，原始响应：\n"
            + json.dumps(resp_json, ensure_ascii=False)[:1500]
        )
    # 立即打印取到的 task_id，便于肉眼核对（豆包真实 task_id 应以 cgt- 开头）。
    print(f"[豆包Seedance] task_id = {task_id}，检索模型 = {GET_MODEL}")
    return task_id


def _require_api_key(api_key):
    if not api_key.strip():
        raise RuntimeError("请填写 DMXAPI 的 api_key")


def _pick_model(custom_model):
    """新节点无 model 下拉：custom_model 优先，否则回退模块默认生成模型。"""
    return custom_model.strip() if custom_model.strip() else GENERATE_MODEL


def _resolve_frame(tensor, url_text, which):
    """首/尾帧二选一来源：IMAGE 张量优先，否则文本框 URL/路径；都没有则报错。"""
    if tensor is not None:
        return _tensor_to_data_url(tensor)
    if url_text and url_text.strip():
        return _resolve_media_url(url_text, True, which)
    raise RuntimeError(f"[豆包Seedance] 首尾帧生视频需要{which}：请接 IMAGE 或填写对应 URL/路径")


def _gen_outputs():
    """5 个生成节点统一的输出：task_id + 固定 get_model。"""
    return [
        io.String.Output("task_id", display_name="任务ID"),
        io.String.Output("get_model", display_name="检索模型"),
    ]


def _looks_like_id(value):
    """粗判一个字符串是否像任务 id。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if len(v) < 6 or len(v) > 80:
        return False
    # 任务 id 多为：cgt-xxx / 纯数字 / UUID 形式，且不含空格、不是 url
    if " " in v or v.startswith("http"):
        return False
    return True


def _deep_find_id(obj):
    """递归在响应里找形如 id 的值，键名优先 video_id/task_id/id。"""
    priority_keys = ("video_id", "task_id", "request_id", "id")
    if isinstance(obj, dict):
        for k in priority_keys:
            if k in obj and _looks_like_id(str(obj[k])):
                return str(obj[k])
        for v in obj.values():
            found = _deep_find_id(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_id(v)
            if found:
                return found
    return None


def _deep_find_cgt_id(obj):
    """递归找首个以 'cgt-' 开头的任务 id（豆包 Seedance 真实 task_id 形如 cgt-xxx）。

    优先于通用查找：避免误抓 OpenAI-Responses 风格的顶层 envelope id（如 resp-xxx）——
    那种 id 喂给获取端会查不到任务。找不到 cgt- 时返回 None，交回通用兜底。
    """
    if isinstance(obj, str):
        s = obj.strip()
        return s if s.startswith("cgt-") and _looks_like_id(s) else None
    if isinstance(obj, dict):
        for v in obj.values():
            found = _deep_find_cgt_id(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_cgt_id(v)
            if found:
                return found
    return None


def _peel_output_text(resp_json):
    """取 output[0].content[0].text 并尝试 json.loads，失败返回原字符串。"""
    try:
        text = resp_json["output"][0]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _extract_task_id(resp_json):
    """从生成响应里提取任务 id。

    豆包真实 task_id 形如 cgt-xxx。提取顺序：
      0) 优先找 cgt- 前缀的 id（先看内嵌 text，再看整体），避免误抓顶层
         envelope id（如 OpenAI-Responses 风格的 resp-xxx）
      1) 顶层明确键 → 2) data.* → 3) 内嵌 text → 4) 全局递归兜底
    """
    # 内嵌 text 里的 cgt- 优先（豆包 task_id 常藏在 output[0].content[0].text 内嵌 JSON 里，
    # 此时它在原始响应里只是个字符串，直接递归整体看不到，必须先剥出来）
    inner = _peel_output_text(resp_json)
    for scope in (inner, resp_json):
        cgt = _deep_find_cgt_id(scope)
        if cgt:
            return cgt

    if isinstance(resp_json, dict):
        # 1) 顶层常见键
        for k in ("video_id", "task_id", "id", "request_id"):
            if k in resp_json and _looks_like_id(str(resp_json[k])):
                return str(resp_json[k])
        # 2) data.task_id
        data = resp_json.get("data")
        if isinstance(data, dict):
            for k in ("video_id", "task_id", "id"):
                if k in data and _looks_like_id(str(data[k])):
                    return str(data[k])
        # 3) output[0].content[0].text 内嵌 JSON
        if inner is not None:
            found = _deep_find_id(inner)
            if found:
                return found
    # 4) 全局递归兜底
    return _deep_find_id(resp_json)


def _deep_find_video_url(obj):
    """递归找首个看起来像视频地址的 http url。"""
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("http") and (".mp4" in s.lower() or "video" in s.lower()):
            return s
        return None
    if isinstance(obj, dict):
        # 优先明确键（仍要求值看起来像视频地址，避免误抓元数据 url）
        for k in ("video_url", "watermark_video_url", "download_url", "url"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                low = v.lower()
                if k in ("video_url", "watermark_video_url") or ".mp4" in low or "video" in low:
                    return v
        for v in obj.values():
            found = _deep_find_video_url(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_video_url(v)
            if found:
                return found
    return None


def _extract_video_url(resp_json):
    """
    从检索响应解析 (status, video_url, message)。
    status 取值统一为大写：SUCCEEDED / PENDING / RUNNING / FAILED / UNKNOWN

    豆包风格：video_url 藏在 output[0].content[0].text 内嵌 JSON 的 content.video_url。
    """
    status = "UNKNOWN"
    message = ""

    inner = _peel_output_text(resp_json)
    candidates = [inner, resp_json]

    for c in candidates:
        if not isinstance(c, dict):
            continue
        # 记录消息
        if c.get("message"):
            message = str(c.get("message"))
        # 扁平 video_url —— 拿到即成功，立即返回
        if isinstance(c.get("video_url"), str) and c["video_url"].startswith("http"):
            return ("SUCCEEDED", c["video_url"], message)
        # 嵌套 content.video_url（豆包 Seedance 风格）
        content = c.get("content")
        if (isinstance(content, dict)
                and isinstance(content.get("video_url"), str)
                and content["video_url"].startswith("http")):
            return ("SUCCEEDED", content["video_url"], message)
        # 状态：FAILED 立即返回（避免被外层 envelope 的状态覆盖）；
        # 其余只在尚未确定时记录，不让后一个候选清掉前一个的明确状态
        raw_status = c.get("task_status") or c.get("status")
        if isinstance(raw_status, str):
            s = raw_status.upper()
            if s == "FAILED":
                return ("FAILED", None, message or "任务失败")
            if status == "UNKNOWN":
                status = s

    # 递归兜底找 url
    url = _deep_find_video_url(resp_json)
    if url:
        return ("SUCCEEDED", url, message)

    return (status, None, message)


def _download(url, filename_prefix, api_key, max_retries=18, base_interval=4, max_interval=10):
    """下载视频到 output 目录，返回 (绝对路径, 文件名, 子目录)。

    返回 video_url 时文件可能还没同步到 CDN 边缘节点，生成后立刻下载会得到 404；
    过几十秒就绪后同一 URL 即 200。因此对 404 / 5xx / 连接错误做退避重试
    （间隔递增），默认最多 18 次、间隔 4→10s，总等待约 2 分钟。
    """
    output_dir = folder_paths.get_output_directory()
    full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
        filename_prefix, output_dir, 0, 0
    )
    os.makedirs(full_output_folder, exist_ok=True)
    file = f"{filename}_{counter:05}_.mp4"
    save_path = os.path.join(full_output_folder, file)

    # 部分 URL 把路径里的 "/" 编码成了 "%2F"。解码 path、保留 query（签名参数不能动）。
    parts = urlsplit(url)
    decoded = urlunsplit(parts._replace(path=unquote(parts.path)))
    candidates = [decoded] if decoded == url else [decoded, url]

    def _get(u, with_auth=False):
        headers = _headers(api_key) if with_auth else None
        return requests.get(u, stream=True, timeout=120, headers=headers)

    print(f"[豆包Seedance] 下载地址：{decoded}")

    last_status = None
    last_err = None
    good = None

    for attempt in range(1, max_retries + 1):
        for u in candidates:
            try:
                resp = _get(u)
                # 鉴权问题：带 token 再试一次该地址
                if resp.status_code in (401, 403):
                    resp = _get(u, with_auth=True)
                if resp.status_code in (200, 206):
                    good = resp
                    break
                last_status = resp.status_code  # 404 / 5xx 等，继续重试
            except requests.RequestException as e:
                last_err = e  # 连接抖动，继续重试
        if good is not None:
            break
        if attempt < max_retries:
            interval = min(base_interval + (attempt - 1), max_interval)
            why = f"HTTP {last_status}" if last_status else f"{last_err}"
            print(f"[豆包Seedance] 第 {attempt}/{max_retries} 次下载未就绪（{why}），等待 {interval}s…")
            time.sleep(interval)

    if good is None:
        raise RuntimeError(
            f"视频已生成但下载失败（CDN 可能尚未就绪或链接已变化）。"
            f"最后状态：{last_status or last_err}，URL：{decoded}"
        )

    with open(save_path, "wb") as f:
        for chunk in good.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    return save_path, file, subfolder


# ───────────────────────────── 节点 1：文生视频 ─────────────────────────────


class DoubaoSeedanceTextToVideo(io.ComfyNode):
    """① 文生视频：仅提示词。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DoubaoSeedanceTextToVideo",
            display_name="🎬豆包 Seedance 文生视频",
            category="DMXAPI/豆包视频模型",
            inputs=[
                *_common_gen_inputs(
                    "写实风格，晴朗的蓝天之下，一大片白色的雏菊花田，镜头逐渐拉近，"
                    "最终定格在一朵雏菊花的特写上，花瓣上有几颗晶莹的露珠"
                ),
            ],
            outputs=_gen_outputs(),
        )

    @classmethod
    def execute(cls, prompt, api_key, base_url, ratio, resolution, duration,
                generate_audio, seed, watermark, custom_model=""):
        _require_api_key(api_key)
        actual_model = _pick_model(custom_model)

        content = [_text_item(prompt)]
        print("[豆包Seedance] 模式：文生视频")

        payload = _seedance_top_level(actual_model, content, ratio, resolution,
                                      duration, generate_audio, seed, watermark)
        task_id = _submit_task(actual_model, base_url, api_key, payload)
        return io.NodeOutput(task_id, GET_MODEL)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # 每次都重新执行（确保每次运行真正发起新任务）
        return random.random()


# ───────────────────────────── 节点 2：首帧生视频 ─────────────────────────────


class DoubaoSeedanceFirstFrame(io.ComfyNode):
    """② 首帧生视频（图生视频）：提示词 + 一张首帧图（接 IMAGE 或填 image_url）。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DoubaoSeedanceFirstFrame",
            display_name="🎬豆包 Seedance 首帧生视频",
            category="DMXAPI/豆包视频模型",
            inputs=[
                *_common_gen_inputs("图中小狗对着镜头说\"茄子\"，360度环绕运镜"),
                io.Image.Input("first_frame", display_name="首帧图片", optional=True,
                               tooltip="首帧图片（IMAGE）。不接则用下面的首帧图片链接"),
                io.String.Input("image_url", display_name="首帧图片链接", default="", optional=True,
                                tooltip="首帧图片 URL / 本地路径 / 素材ID / data URL；已接首帧图片时此项被忽略"),
            ],
            outputs=_gen_outputs(),
        )

    @classmethod
    def execute(cls, prompt, api_key, base_url, ratio, resolution, duration,
                generate_audio, seed, watermark, custom_model="",
                first_frame=None, image_url=""):
        _require_api_key(api_key)
        actual_model = _pick_model(custom_model)

        # 首帧来源：连线的 IMAGE 优先，其次文本框（链接/本地路径/素材ID）
        if first_frame is not None:
            image_data_url = _tensor_to_data_url(first_frame)
            print("[豆包Seedance] 模式：首帧生视频（IMAGE 输入）")
        elif image_url and image_url.strip():
            image_data_url = _resolve_media_url(image_url, True, "首帧图片")
            print("[豆包Seedance] 模式：首帧生视频（图片链接/路径）")
        else:
            raise RuntimeError("[豆包Seedance] 首帧生视频需要一张首帧图：请接 IMAGE 或填写 image_url"
                               "（纯文生视频请改用「🎬豆包 Seedance 文生视频」节点）")

        content = [_text_item(prompt), _image_item(image_data_url, "first_frame")]

        payload = _seedance_top_level(actual_model, content, ratio, resolution,
                                      duration, generate_audio, seed, watermark)
        task_id = _submit_task(actual_model, base_url, api_key, payload)
        return io.NodeOutput(task_id, GET_MODEL)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return random.random()


# ───────────────────────────── 通用：获取节点（6 功能共用）─────────────────────────────


class DoubaoSeedanceFetch(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DoubaoSeedanceFetch",
            display_name="🎬豆包 Seedance 获取",
            category="DMXAPI/豆包视频模型",
            is_output_node=True,
            inputs=[
                io.String.Input("task_id", display_name="任务ID", force_input=True),
                io.String.Input("get_model", display_name="检索模型", force_input=True),
                io.String.Input("api_key", display_name="密钥(api_key)", default=""),
                io.String.Input("base_url", display_name="接口地址", default=DEFAULT_BASE),
                io.Int.Input("max_wait_seconds", display_name="最大等待(秒)", default=300, min=10, max=1800),
                io.Int.Input("poll_interval", display_name="轮询间隔(秒)", default=6, min=2, max=60),
                io.String.Input("filename_prefix", display_name="保存名前缀", default="doubao/seedance"),
            ],
            outputs=[
                io.Video.Output("video", display_name="视频"),
                io.String.Output("video_url", display_name="视频链接"),
            ],
        )

    @classmethod
    def execute(cls, task_id, get_model, api_key, base_url,
                max_wait_seconds, poll_interval, filename_prefix):
        if not api_key.strip():
            raise RuntimeError("请填写 DMXAPI 的 api_key")
        if not task_id.strip():
            raise RuntimeError("task_id 为空——请先连接生成节点（如🎬豆包 Seedance 文生视频 / 首帧生视频）")

        # get_model 容错：若上游没接或为空，回退到豆包默认检索模型
        get_model = get_model.strip() if get_model.strip() else GET_MODEL

        url = base_url.strip().rstrip("/") + "/v1/responses"
        body = {"model": get_model, "input": task_id.strip()}

        deadline = time.time() + max_wait_seconds
        attempt = 0
        last_status = "UNKNOWN"

        while time.time() < deadline:
            attempt += 1
            try:
                resp = requests.post(url, headers=_headers(api_key), json=body, timeout=120)
                resp.raise_for_status()
                resp_json = resp.json()
            except Exception as e:  # 单次网络抖动不致命，继续轮询
                print(f"[豆包Seedance] 第 {attempt} 次查询出错（忽略，继续轮询）：{e}")
                time.sleep(poll_interval)
                continue

            status, video_url, message = _extract_video_url(resp_json)
            last_status = status

            if status == "FAILED":
                raise RuntimeError(f"[豆包Seedance] 生成失败：{message}")

            if video_url:
                print(f"[豆包Seedance] 生成完成，开始下载：{video_url}")
                save_path, file, subfolder = _download(video_url, filename_prefix, api_key)
                video = InputImpl.VideoFromFile(save_path)
                print(f"[豆包Seedance] 已保存到 output/{subfolder}/{file}")
                return io.NodeOutput(
                    video,
                    video_url,
                    ui=ui.PreviewVideo([ui.SavedResult(file, subfolder, io.FolderType.output)]),
                )

            # 任务报告成功却拿不到 URL：明确报错，不要一直轮询到超时让人误以为还在跑
            if status == "SUCCEEDED":
                raise RuntimeError(
                    "[豆包Seedance] 任务已成功但未能从响应中解析出视频 URL，原始响应：\n"
                    + json.dumps(resp_json, ensure_ascii=False)[:1500]
                )

            print(f"[豆包Seedance] 第 {attempt} 次查询：状态 {status}，等待 {poll_interval}s …")
            time.sleep(poll_interval)

        raise RuntimeError(
            f"[豆包Seedance] 等待超时（{max_wait_seconds}s），最后状态：{last_status}。"
            f"可调大 max_wait_seconds 后重试，或检查 task_id / 检索模型是否正确。"
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return random.random()


# ───────────────── 节点 3~6：首尾帧 / 视频编辑 / 视频延长 / 多模态参考 ─────────────────
# 这 4 个生成节点共用顶层参数（prompt/api_key/base_url/ratio/resolution/duration/
# generate_audio/seed/watermark/custom_model），差异仅在各自的媒体输入与 input 数组拼法。


def _common_gen_inputs(prompt_default=""):
    """6 个生成节点共用的顶层参数输入（顺序固定，作为工作流 widgets_values 对照基准）。

    首尾帧/参考类场景常用 adaptive（按首帧/参考比例自动适配），故这里默认 adaptive。
    prompt_default 由各节点传入场景化的默认提示词。
    """
    return [
        io.String.Input("custom_model", display_name="模型", default=GENERATE_MODEL,
                        tooltip="默认 doubao-seedance-2-0-260128；如需用别的模型名可改此处"),
        io.String.Input("prompt", display_name="提示词", multiline=True, default=prompt_default),
        io.String.Input("api_key", display_name="密钥(api_key)", default=""),
        io.String.Input("base_url", display_name="接口地址", default=DEFAULT_BASE),
        io.Combo.Input(
            "ratio", display_name="宽高比",
            options=["adaptive", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"],
            default="adaptive",
        ),
        io.Combo.Input("resolution", display_name="分辨率", options=["480p", "720p", "1080p"], default="480p"),
        io.Int.Input("duration", display_name="时长(秒)", default=5, min=4, max=15),
        io.Boolean.Input("generate_audio", display_name="生成音频", default=True),
        io.Int.Input("seed", display_name="种子", default=-1, min=-1, max=4294967295, control_after_generate=True),
        io.Boolean.Input("watermark", display_name="水印", default=False),
    ]


class DoubaoSeedanceFirstLastFrame(io.ComfyNode):
    """③ 首尾帧生视频：提示词 + 首帧图 + 尾帧图（两帧都必填）。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DoubaoSeedanceFirstLastFrame",
            display_name="🎬豆包 Seedance 首尾帧",
            category="DMXAPI/豆包视频模型",
            inputs=[
                *_common_gen_inputs("镜头从首帧画面平滑过渡到尾帧画面，自然流畅的运镜"),
                io.Image.Input("first_frame", display_name="首帧图片", optional=True,
                               tooltip="首帧图片（IMAGE）。不接则用首帧图片链接"),
                io.String.Input("first_frame_url", display_name="首帧图片链接", default="", optional=True,
                                tooltip="首帧图片 URL / 本地路径 / 素材ID；已接首帧图片时忽略"),
                io.Image.Input("last_frame", display_name="尾帧图片", optional=True,
                               tooltip="尾帧图片（IMAGE）。不接则用尾帧图片链接"),
                io.String.Input("last_frame_url", display_name="尾帧图片链接", default="", optional=True,
                                tooltip="尾帧图片 URL / 本地路径 / 素材ID；已接尾帧图片时忽略"),
            ],
            outputs=_gen_outputs(),
        )

    @classmethod
    def execute(cls, prompt, api_key, base_url, ratio, resolution, duration,
                generate_audio, seed, watermark, custom_model="",
                first_frame=None, first_frame_url="", last_frame=None, last_frame_url=""):
        _require_api_key(api_key)
        actual_model = _pick_model(custom_model)

        first_url = _resolve_frame(first_frame, first_frame_url, "首帧")
        last_url = _resolve_frame(last_frame, last_frame_url, "尾帧")
        content = [
            _text_item(prompt),
            _image_item(first_url, "first_frame"),
            _image_item(last_url, "last_frame"),
        ]
        print("[豆包Seedance] 模式：首尾帧生视频")

        payload = _seedance_top_level(actual_model, content, ratio, resolution,
                                      duration, generate_audio, seed, watermark)
        task_id = _submit_task(actual_model, base_url, api_key, payload)
        return io.NodeOutput(task_id, GET_MODEL)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return random.random()


class DoubaoSeedanceReference(io.ComfyNode):
    """④⑤⑥合一：参考生成视频（视频编辑 / 视频延长 / 多模态参考）。

    动态增长 socket（连一个冒一个，io.Autogrow）：
      图 image_1..9（IMAGE 接口）/ 视频 video_1..3（VIDEO 接口，接加载视频自动上传换 URL）/
      视频链接 video_url_1..3（STRING，填 URL/素材ID）/ 音频 audio_1..3（AUDIO 接口）/
      素材 asset_1..9（STRING 素材ID）。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DoubaoSeedanceReference",
            display_name="🎬豆包 Seedance 参考生成视频",
            category="DMXAPI/豆包视频模型",
            inputs=[
                *_common_gen_inputs("[图1]中的主体，按提示词运动；可参考[视频1]的运镜"),
                io.Autogrow.Input("ref_images", template=io.Autogrow.TemplateNames(
                    io.Image.Input("image", display_name="图片"),
                    names=[f"image_{i}" for i in range(1, 10)], min=0)),
                io.Autogrow.Input("ref_videos", template=io.Autogrow.TemplateNames(
                    io.Video.Input("video", display_name="视频",
                                   tooltip="接「加载视频/创建视频」节点。本地视频会自动匿名上传到临时文件床"
                                           "换取公网 URL 再发送（有隐私风险，私密视频请改填「视频链接」）"),
                    names=[f"video_{i}" for i in range(1, 4)], min=0)),
                io.Autogrow.Input("ref_video_urls", template=io.Autogrow.TemplateNames(
                    io.String.Input("video_url", display_name="视频链接",
                                    tooltip="填公网视频 URL 或 asset:// 素材ID（不经第三方上传，隐私安全）；"
                                            "与上面「视频」二选一或混用，合计≤3"),
                    names=[f"video_url_{i}" for i in range(1, 4)], min=0)),
                io.Autogrow.Input("ref_audios", template=io.Autogrow.TemplateNames(
                    io.Audio.Input("audio", display_name="音频"),
                    names=[f"audio_{i}" for i in range(1, 4)], min=0)),
                io.Autogrow.Input("ref_assets", template=io.Autogrow.TemplateNames(
                    io.String.Input("asset", display_name="素材ID",
                                    tooltip="asset:// 素材ID（裸ID会自动补前缀）；本期仅支持图片类素材"),
                    names=[f"asset_{i}" for i in range(1, 10)], min=0)),
            ],
            outputs=_gen_outputs(),
        )

    @classmethod
    def execute(cls, prompt, api_key, base_url, ratio, resolution, duration,
                generate_audio, seed, watermark, custom_model="",
                ref_images=None, ref_videos=None, ref_audios=None, ref_assets=None,
                ref_video_urls=None):
        _require_api_key(api_key)
        actual_model = _pick_model(custom_model)

        def _vals(d):
            # Autogrow 值是 dict（未连接时为空 dict 或 None）；按 socket 名数值序取值
            return [d[k] for k in sorted((d or {}).keys(), key=_socket_index)]

        imgs = [_image_item(_tensor_to_data_url(t), "reference_image")
                for t in _vals(ref_images) if t is not None]
        # 参考视频两组：VideoInput 接口（接加载视频→匿名上传换 URL） + 视频链接文本（URL/素材ID）
        vids = [_video_item(_video_source_to_url(v), "reference_video")
                for v in _vals(ref_videos) if v is not None]
        vids += [_video_item(_resolve_media_url(u, False, "参考视频"), "reference_video")
                 for u in _vals(ref_video_urls) if isinstance(u, str) and u.strip()]
        auds = [_audio_item(_audio_input_to_data_url(a), "reference_audio")
                for a in _vals(ref_audios) if a is not None]

        # 素材ID：本期仅支持【图片类】素材（按 reference_image 提交）。
        # 官方节点靠字节自家元数据端点查素材真实类型再分图/视频/音频，DMXAPI 没有该端点，
        # 客户端无法判别 → 统一当图。裸 ID 自动补 asset:// 前缀（同官方）。
        def _norm_asset(s):
            s = s.strip()
            return s if s.startswith(("asset://", "http://", "https://")) else f"asset://{s}"
        assets = [_image_item(_norm_asset(s), "reference_image")
                  for s in _vals(ref_assets) if isinstance(s, str) and s.strip()]

        n_img = len(imgs) + len(assets)
        if n_img > 9:
            raise RuntimeError(f"[豆包Seedance] 参考图 + 素材最多 9，当前 {n_img}")
        if len(vids) > 3:
            raise RuntimeError(f"[豆包Seedance] 参考视频最多 3（视频+视频链接合计），当前 {len(vids)}")
        if len(auds) > 3:
            raise RuntimeError(f"[豆包Seedance] 参考音频最多 3，当前 {len(auds)}")
        if not imgs and not vids and not assets:
            raise RuntimeError("[豆包Seedance] 参考生成视频至少需要 1 张图 / 1 段视频 / 1 个素材ID")

        content = [_text_item(prompt), *imgs, *assets, *vids, *auds]
        print(f"[豆包Seedance] 模式：参考生成视频（图{len(imgs)} 素材{len(assets)} "
              f"视频{len(vids)} 音频{len(auds)}）")

        payload = _seedance_top_level(actual_model, content, ratio, resolution,
                                      duration, generate_audio, seed, watermark)
        task_id = _submit_task(actual_model, base_url, api_key, payload)
        return io.NodeOutput(task_id, GET_MODEL)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return random.random()


# ───────────────────────────── 注册 ─────────────────────────────


class DoubaoSeedanceExtension(ComfyExtension):
    async def get_node_list(self):
        return [
            DoubaoSeedanceTextToVideo,
            DoubaoSeedanceFirstFrame,
            DoubaoSeedanceFirstLastFrame,
            DoubaoSeedanceReference,
            DoubaoSeedanceFetch,
        ]


async def comfy_entrypoint():
    return DoubaoSeedanceExtension()
