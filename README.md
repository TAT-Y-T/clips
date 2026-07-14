# 四路视频时间对齐与片段导出工具

这是一个基于 Python 和 FFmpeg 的多视角视频处理脚本，用于从四路同步录制的视频中，按照时间戳和标注区间导出时间对齐的视频片段。

脚本支持以下输入视角：

- `primary`：主视角
- `left_hand`：左手视角
- `right_hand`：右手视角
- `secondary`：辅助视角

核心特点：

- 根据四路视频的 wall-clock 时间计算公共重叠区间；
- 将主视频标注时间映射到其他视角，保证片段对应同一真实时间；
- 可选地将 CSV 时间戳与 MP4 中的实际帧 PTS 进行校验；
- 支持导出整段对齐视频或按标注导出多个片段；
- 支持批量扫描多个录制目录；
- 支持左眼区域裁剪和四路 2×2 网格视频；
- 生成 JSON manifest、CSV 对齐报告和 Markdown 对齐报告；
- 支持断点续跑，默认跳过已经生成的有效文件。

## 目录

- [运行环境](#运行环境)
- [输入目录结构](#输入目录结构)
- [时间戳文件格式](#时间戳文件格式)
- [标注文件格式](#标注文件格式)
- [快速开始](#快速开始)
- [命令行参数](#命令行参数)
- [处理模式](#处理模式)
- [输出目录结构](#输出目录结构)
- [时间对齐逻辑](#时间对齐逻辑)
- [输出报告](#输出报告)
- [常见用法](#常见用法)
- [故障排查](#故障排查)

## 运行环境

### 软件要求

- Python 3.9 或更高版本；
- FFmpeg；
- FFprobe（通常随 FFmpeg 一起安装）。

检查依赖：

```bash
python3 --version
ffmpeg -version
ffprobe -version
```

脚本只使用 Python 标准库，不需要额外安装第三方 Python 包。

## 输入目录结构

普通原始录制目录应至少包含四路 MP4 和 `_timestamps/` 目录：

```text
recording_dir/
├── recording_primary.mp4
├── recording_left_hand.mp4
├── recording_right_hand.mp4
├── recording_secondary.mp4
├── annotation.json                 # 可选；导出片段时需要
└── _timestamps/
    ├── timestamps_primary.csv
    ├── timestamps_left_hand.csv
    ├── timestamps_right_hand.csv
    └── timestamps_secondary.csv
```

脚本也支持时间戳文件的其他命名族：

| `--timeline` 值 | 文件名模板 |
|---|---|
| `src` | `pts_src_{view}.csv` |
| `encoded` | `pts_encoded_{view}.csv` |
| `decoded` | `pts_decoded_{view}.csv` |
| `timestamps` | `timestamps_{view}.csv` |

如果视频位于子目录中，例如 `upload/recording_primary.mp4`，可以使用 `--input-subdir upload`。

### 已对齐输入目录

如果目录已经包含裁剪后的四路对齐视频以及对应的对齐时间戳，则可以直接进行二次片段导出：

```text
aligned/
├── recording_primary.mp4
├── recording_left_hand.mp4
├── recording_right_hand.mp4
├── recording_secondary.mp4
├── aligned_timestamps_primary.csv
├── aligned_timestamps_left_hand.csv
├── aligned_timestamps_right_hand.csv
└── aligned_timestamps_secondary.csv
```

当目录具有上述结构且没有 `_timestamps/` 时，脚本会自动识别为已对齐输入，并直接使用标注中的相对秒数进行裁剪。

## 时间戳文件格式

每个时间戳 CSV 至少需要包含以下三列：

```csv
frame,pts_ns,wallclock_ns
0,0,1710000000000000000
1,33333333,1710000000033333333
2,66666666,1710000000066666666
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---:|---|
| `frame` | 整数 | 源视频帧编号 |
| `pts_ns` | 整数 | 帧的 PTS，单位为纳秒 |
| `wallclock_ns` | 整数 | 采集时的绝对墙上时钟，单位为纳秒 |

读取时会忽略空行和缺少必要字段的行。脚本会检查时间戳是否有效，并删除末尾不再递增的异常行。文件为空或所有行均无效时会报错。

## 标注文件格式

脚本支持两种 JSON 格式。

### 格式一：`section` 格式

```json
{
  "section": [
    {
      "id": 1,
      "time": [[12.5, 18.0], [30.0, 35.5]],
      "description": "拿起物体并放入盒中"
    },
    {
      "id": 2,
      "time": [[42.0, 49.0]],
      "description": "打开抽屉"
    }
  ]
}
```

其中每个时间区间必须是 `[开始秒数, 结束秒数]`，且开始时间必须小于结束时间。一个 section 可以包含多个区间。

注意：当前代码使用 `section_<id>/` 作为输出目录，并在该目录中使用固定文件名。如果同一个 section 的 `time` 包含多个区间，后续区间可能覆盖前一个区间的同名输出。若需要保留同一 section 的多个区间，建议为每个区间使用不同的 section `id`，或在代码中将 `range_index` 加入输出路径。

### 格式二：`instances.segments` 格式

```json
{
  "instances": {
    "segments": [
      {
        "id": 1,
        "group": [
          {"startTime": 12.5, "endTime": 18.0}
        ],
        "attributes": {
          "action": "拿起物体",
          "target": "盒子"
        }
      }
    ]
  }
}
```

该格式会自动转换为内部的 section 结构。`attributes` 中的非空值会按换行拼接，写入每个片段目录下的 `describe.txt`。

当检测到 `instances.segments` 格式时，脚本默认启用：

- 左眼裁剪输出；
- 简化的输出文件名；
- 默认输出目录 `clip/`（仅在没有显式指定 `--output-dir` 时生效）。

## 快速开始

假设脚本保存为 `extract_clips.py`，并且当前目录就是一个录制目录：

```bash
python3 extract_clips.py /path/to/recording_dir
```

默认行为：

1. 读取四路 MP4；
2. 读取 `_timestamps/timestamps_*.csv`；
3. 将 CSV 时间戳与 MP4 帧 PTS 匹配；
4. 读取 `annotation.json`；
5. 将标注时间按主视角时间基准映射到四路视频；
6. 在 `<recording_dir>/clips/` 下导出片段；
7. 写入 manifest 和对齐报告。

建议先使用 dry-run 检查计划：

```bash
python3 extract_clips.py /path/to/recording_dir --dry-run
```

`--dry-run` 会生成计划文件，但不会调用 FFmpeg 导出视频。

## 命令行参数

运行以下命令查看参数：

```bash
python3 extract_clips.py --help
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `recording_dir` | `/Users/tong/Downloads/0002_1782611070` | 录制目录或包含多个录制目录的根目录 |
| `--annotation PATH` | 自动查找 | 标注 JSON；默认查找 `annotation.json`，也兼容两个历史拼写文件名 |
| `--output-dir PATH` | `<recording_dir>/clips` | 输出目录 |
| `--input-subdir NAME` | 无 | 从录制目录的指定子目录读取视频 |
| `--dry-run` | 关闭 | 只生成 manifest，不运行 FFmpeg |
| `--force` | 关闭 | 强制重新生成已有输出 |
| `--no-timer` | 关闭 | 不输出耗时信息 |
| `--align-full` | 关闭 | 导出四路公共 wall-clock 重叠区间的完整对齐视频 |
| `--reencode` | 关闭 | 使用 H.264/AAC 重编码，提高切点精度 |
| `--timeline NAME` | `timestamps` | 选择时间戳文件族：`src`、`encoded`、`decoded`、`timestamps` |
| `--no-validate-mp4-frames` | 关闭 | 不将 CSV 行与 MP4 实际帧 PTS 匹配 |
| `--annotation-timebase NAME` | `raw-primary` | 标注时间基准：`raw-primary` 或 `aligned` |
| `--include-only-left` | 关闭 | 额外导出主视角的左眼裁剪视频 |
| `--simple-output-names` | 关闭 | 使用 `primary.mp4` 等简化名称 |
| `--make-grid` | 关闭 | 每个 section 生成 2×2 网格视频 |
| `--grid-cell-width N` | `960` | 网格单元宽度 |
| `--grid-cell-height N` | `540` | 网格单元高度 |

## 处理模式

### 1. 按标注导出片段

```bash
python3 extract_clips.py /data/session_001 \
  --annotation /data/session_001/annotation.json \
  --output-dir /data/session_001/clips \
  --reencode
```

每个 section 的时间区间都会规划一组四路输出。四路视频使用同一个 wall-clock 时间区间，因此不同视角的相对视频时间可以不同，但实际采集时刻一致。当前输出路径按 `section_<id>/` 组织，多个区间共用同一个 section ID 时请注意文件覆盖问题。

### 2. 导出完整对齐视频

```bash
python3 extract_clips.py /data/session_001 --align-full --reencode
```

脚本会计算四路视频共有的 wall-clock 区间，并在 `aligned/` 下导出四路视频，同时生成：

- `alignment_manifest.json`；
- `alignment_report.md`。

### 3. 使用已对齐视频导出片段

```bash
python3 extract_clips.py /data/session_001/aligned \
  --annotation /data/session_001/annotation.json
```

此时标注区间被视为已对齐视频的相对秒数，不再重新计算四路视频之间的 wall-clock 映射。

### 4. 导出左眼裁剪片段

```bash
python3 extract_clips.py /data/session_001 \
  --include-only-left \
  --reencode
```

默认使用 FFmpeg 滤镜：

```text
crop=1920:1200:160:0
```

该滤镜从主视角视频中裁剪出 `1920×1200` 区域，起点为 `(160, 0)`。如需适配其他输入分辨率，应修改代码中的 `LEFT_ONLY_CROP_FILTER`。

### 5. 生成四路 2×2 网格视频

```bash
python3 extract_clips.py /data/session_001 \
  --make-grid \
  --grid-cell-width 960 \
  --grid-cell-height 540
```

网格布局固定为：

```text
┌─────────────┬─────────────┐
│ primary     │ secondary   │
├─────────────┼─────────────┤
│ left_hand   │ right_hand  │
└─────────────┴─────────────┘
```

每一路会先按指定尺寸缩放并补边，然后使用 CFR 30 FPS 合成。

### 6. 批量处理

将根目录传给脚本即可递归查找录制目录：

```bash
python3 extract_clips.py /data/all_sessions --output-dir /data/clips_output
```

批处理时，脚本会：

- 自动查找包含 `_timestamps/` 和四路 MP4 的目录；
- 自动保留相对目录结构；
- 缺少标注文件的目录会跳过并打印提示；
- 已完成的目录会跳过，除非指定 `--force`。

## 输出目录结构

普通片段导出示例：

```text
clips/
├── clip_manifest.json
├── alignment_report.csv
├── alignment_report.md
└── section_1/
    ├── recording_primary.mp4
    ├── recording_left_hand.mp4
    ├── recording_right_hand.mp4
    ├── recording_secondary.mp4
    ├── recording_primary_left.mp4       # 使用 --include-only-left 时生成
    ├── combined_grid.mp4                # 使用 --make-grid 时生成
    └── describe.txt                     # section 有 description 时生成
```

使用 `--simple-output-names` 后，四路文件名会变为 `primary.mp4`、`secondary.mp4`、`left_hand.mp4`、`right_hand.mp4`，左眼裁剪文件名为 `only_left.mp4`。

## 时间对齐逻辑

脚本的对齐基准是 `wallclock_ns`，而不是各视频自身从零开始的 PTS。

### 原始录制模式

对每一路视频：

1. 加载所选时间戳 CSV；
2. 使用 FFprobe 读取 MP4 视频包的 `pts_time`；
3. 将 CSV 中的相对 PTS 与 MP4 PTS 进行匹配，默认允许误差为 20 ms；
4. 计算四路视频的公共 wall-clock 区间：
   - 开始时间取四路开始时间的最大值；
   - 结束时间取四路结束时间的最小值；
5. 将标注的开始、结束时间先转换为主视角 wall-clock，再映射到其他视角；
6. 对超出公共区间的标注进行裁剪；完全没有重叠的区间会被跳过。

### `raw-primary` 与 `aligned`

`--annotation-timebase raw-primary` 表示标注秒数来自原始主视角视频。例如 `[12.5, 18.0]` 表示主视角原始视频的第 12.5 至 18 秒。

`--annotation-timebase aligned` 表示标注秒数来自四路公共对齐区间。例如 `[0, 5]` 表示从公共对齐区间起点开始的前 5 秒。超出对齐视频长度的时间会被限制在有效范围内。

### 帧校验

默认开启 MP4 帧校验。它可以避免时间戳 CSV 与实际编码视频帧序列存在偏差时产生错误切点。

若输入时间戳和 MP4 PTS 无法可靠对应，可使用：

```bash
python3 extract_clips.py /data/session_001 --no-validate-mp4-frames
```

此时脚本直接使用 CSV 行进行映射，但切点精度和可靠性取决于输入 CSV 的质量。

## 输出报告

### `clip_manifest.json`

记录本次处理的完整计划，包括：

- 输入、输出路径；
- 选用的时间戳族；
- 是否进行了 MP4 帧校验；
- 每个 section 的原始和有效时间；
- 每一路的帧号、PTS、wall-clock 和误差；
- 被跳过的区间及原因。

### `alignment_report.csv`

逐条列出每个 section 在每个视角的开始/结束切点，适合后续用脚本分析最大对齐误差。

### `alignment_report.md`

提供人类可读的对齐报告，误差以毫秒展示。

### `describe.txt`

当 annotation 中存在描述信息时，写入对应 section 目录，方便数据集制作和人工复核。

## FFmpeg 输出策略

默认情况下，普通视频片段使用 stream copy：

```text
-c copy
```

优点是速度快且不会重复编码；缺点是切点通常受关键帧限制，实际开始/结束位置可能与目标存在偏差。

指定 `--reencode` 后，普通片段使用 H.264/AAC 重编码，参数大致为：

```text
-c:v libx264 -preset veryfast -crf 18 -c:a aac
```

左眼裁剪和网格视频始终需要重新编码，因为它们使用了视频滤镜。

## 常见用法

只生成计划和报告，不导出视频：

```bash
python3 extract_clips.py /data/session_001 --dry-run
```

从 `upload/` 读取视频：

```bash
python3 extract_clips.py /data/session_001 --input-subdir upload
```

使用另一组时间戳：

```bash
python3 extract_clips.py /data/session_001 --timeline decoded
```

强制重建所有输出：

```bash
python3 extract_clips.py /data/session_001 --force --reencode
```

关闭计时输出：

```bash
python3 extract_clips.py /data/session_001 --no-timer
```

## 故障排查

### `No recording folders found`

检查以下内容：

- 目录路径是否正确；
- 是否存在四个文件：`recording_primary.mp4`、`recording_left_hand.mp4`、`recording_right_hand.mp4`、`recording_secondary.mp4`；
- 是否存在 `_timestamps/`；
- 如果视频在子目录中，是否指定了 `--input-subdir`。

### `No timestamp rows found`

检查时间戳 CSV 是否：

- 包含表头 `frame,pts_ns,wallclock_ns`；
- 至少有一行完整数据；
- 数值字段可以转换为整数。

### `No MP4 frames matched timestamp rows within tolerance`

说明 CSV 的 PTS 与 MP4 实际 PTS 差异超过默认 20 ms，常见原因包括时间戳来源不匹配或使用了错误的 `--timeline`。可以先确认时间戳文件族，再尝试 `--no-validate-mp4-frames`。

### `No shared wallclock overlap across all views`

四路视频的 wall-clock 时间范围没有交集。请检查各路设备的系统时钟、录制起止时间和 CSV 内容。

### 输出片段没有生成或被跳过

查看终端日志和 `clip_manifest.json` 中的 `skipped_sections`。常见原因：

- 标注区间完全位于四路视频的公共区间之外；
- 帧量化后开始和结束落在同一帧；
- 输出文件已存在且被识别为完整。此时使用 `--force`。

### 左眼裁剪失败

默认裁剪区域要求输入视频至少足够覆盖 `crop=1920:1200:160:0`。如果输入分辨率不同，需要修改代码中的 `LEFT_ONLY_CROP_FILTER`。

## 注意事项

- 脚本默认的 `recording_dir` 是代码作者本机路径，实际使用时建议显式传入目录；
- 输出目录可能包含较大的视频文件，请提前确认磁盘空间；
- `--reencode` 更精确但速度更慢；
- 标注时间必须与 `--annotation-timebase` 的选择一致；
- 批处理时，缺少 annotation 的目录会被跳过，而不是直接终止整个批次；
- 脚本依赖 `ffprobe` 的 packet PTS，某些特殊封装或损坏视频可能无法提供有效时间戳。

## 许可证

当前代码未声明许可证。如需公开发布或在其他项目中分发，请根据项目实际版权归属补充许可证信息。
