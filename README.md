# 🎬 视频合成 Agent

上传视频、图片与背景音乐，用**自然语言或结构化 JSON** 描述合成规则，AI 规划 + FFmpeg 渲染成片。

支持片段截取、恒速/曲线变速、转场（可启用/关闭）、无损画质输出、背景音乐混音，全程通过 Web 页面操作，实时 SSE 进度推送。

## 功能特性

### 素材
- 支持视频（mp4/mov/mkv/webm/avi）、图片（jpg/png/webp/gif/bmp）、音频（mp3/m4a/aac/wav/ogg/flac）
- 三通道上传：点击/拖拽选择、剪贴板 Ctrl+V 粘贴、服务器素材选择器（适用于无法弹出文件对话框的嵌入式环境）
- 图片素材可设置展示时长，作为静态片段参与成片

### 片段编辑（每个素材独立配置）
- **截取**：任意设置开始/结束点
- **恒速变速**：0.25x ~ 4x，拖动可视化滑线直接调整
- **曲线变速**：可视化折线编辑器——拖动控制点、双击空白加点、双击中间点删点；亦可由函数生成（sin/cos/tan 预设或自定义 `f(u)` 表达式，白名单安全编译），实时预览成片时长
- 配置任一片段后进入**手动模式**：直接生成结构化计划，不经 AI

### 全局配置
- **转场开关**：可启用/关闭；启用时可选 fade / dissolve / 左右擦除 / 上滑入，时长 0~3s，附带 A→B 循环效果演示动画；关闭时片段硬切直连
- **不压缩（无损画质）**：`libx264 qp 0` 数学无损编码（预渲染中间片段与最终成片同步切换，避免二次压缩），文件显著增大

### 合成规则（AI 模式）
- **自然语言**：如"每个视频取中间 4 秒，第二段 2 倍速快放，片段间用 0.5 秒淡入淡出转场，配背景音乐循环对齐成片时长，输出 1080p 30fps"
- **结构化 JSON**：直接给出 CompositionPlan（见下方示例）
- 支持快放/慢放、曲线变速（ramp_in / ramp_out / ease_in_out / 自定义折线）、转场、音乐循环/淡入淡出/保留原声、按音乐时长均分卡点、无损输出等指令

### 配乐与输出
- 背景音乐循环/截断对齐成片时长，可调音量、淡入淡出、按需混入视频原声（变速与转场对齐）
- 输出分辨率/fps 可配置，默认 1080p 30fps

## 架构

```
web/index.html        前端单页（素材管理 + 片段/曲线/转场编辑器 + 进度与结果展示）
app/
  main.py             FastAPI 入口：上传/任务/SSE 进度/结果下载/素材接口
  jobs.py             任务管理与后台执行
  models.py           Pydantic 模型（CompositionPlan / MediaInfo 等，含 null 容错）
  config.py           路径与环境配置
  agent/
    planner.py        规则解析：自然语言 → 结构化计划（DashScope Qwen，校验失败自动重试）
    graph.py / state.py   合成流程编排
  media/
    probe.py          ffprobe 媒体探测
    renderer.py       FFmpeg 渲染管线（片段预渲染 → xfade/concat 拼接 → 配乐混音）
```

渲染管线：每个片段预渲染（截取 + 倍速 + 统一分辨率/fps/pix_fmt）→ xfade 转场链（或 concat 硬切）→ 配乐循环/截断 + 淡入淡出 + 可选原声 amix。曲线变速片段切分为恒速子段后无缝 concat。

## 快速开始

依赖：Python 3.10+、FFmpeg（需含 libx264）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 DashScope API Key（AI 模式需要；纯手动模式可不配）
export DASHSCOPE_API_KEY=sk-xxx        # 在 https://bailian.console.aliyun.com/ 获取
export PLANNER_MODEL=qwen-plus         # 可选，默认 qwen-plus

# 3. 启动
python3 run.py                         # 默认 0.0.0.0:8000

# 4. 打开页面
# http://localhost:8000
```

## 使用方式

1. 上传或从服务器选择视频/图片素材
2. 展开素材列表项，设置片段截取范围与播放方式（匀速/曲线）
3. 在全局配置区设置转场开关、类型与时长，以及是否不压缩输出
4. 填写自然语言规则（AI 模式）或直接提交（已配置片段时自动走手动模式）
5. 观察实时进度，完成后在线播放或下载成片

结构化 JSON 规则示例：

```json
{
  "clips": [
    {"video_index": 0, "start": 2, "duration": 4, "speed": 1},
    {"video_index": 1, "start": 3, "duration": 4, "speed": 2},
    {"video_index": 2, "start": 1, "duration": 4,
     "speed_curve": {"type": "ramp_in", "low": 0.5, "high": 2}}
  ],
  "transition": {"type": "fade", "duration": 0.5},
  "audio": {"loop": true, "volume": 0.8, "fade_in": 1, "fade_out": 2, "keep_original": false},
  "output": {"resolution": [1920, 1080], "fps": 30, "duration_policy": "follow_clips", "lossless": false}
}
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/assets` | 列出服务器素材（`test_assets/` 目录） |
| GET | `/api/assets/file/{name}` | 流式获取素材文件（预览） |
| POST | `/api/jobs` | 创建合成任务（multipart：`videos`/`music` 文件，`video_assets`/`music_asset` 服务器素材名，`rule`、`rule_format`=`text`\|`json`） |
| GET | `/api/jobs/{id}` | 查询任务状态/计划 |
| GET | `/api/jobs/{id}/events` | SSE 进度流（推送至 done/failed） |
| GET | `/api/jobs/{id}/result` | 下载成片 mp4 |

## 测试

`test_assets/` 目录（git 忽略）需自备 `v0.mp4` `v1.mp4` `v2.mp4` `img_a.png` `img_b.png` `music.mp3`：

```bash
python3 test_pipeline.py   # 结构化规则端到端（不经 LLM）
python3 test_curve.py      # 曲线变速
python3 test_speed.py      # 恒速变速
python3 test_llm.py        # LLM 规划（需 DASHSCOPE_API_KEY）
```

## 目录说明

- `uploads/` 用户上传文件（git 忽略）
- `outputs/{job_id}/` 任务产物：中间片段（`tmp/`）与成片（`result.mp4`）（git 忽略）
- `examples/rules.json` 规则示例
