

## 快速开始

假设脚本保存为 `extract_clips.py`，并且当前目录就是一个录制目录：
假设脚本文件为 `clips.py`，并且当前目录就是一个录制目录：

```bash
python3 extract_clips.py /path/to/recording_dir
python3 clips.py /path/to/recording_dir
```

默认行为：
建议先使用 dry-run 检查计划：

```bash
python3 extract_clips.py /path/to/recording_dir --dry-run
python3 clips.py /path/to/recording_dir --dry-run
```

`--dry-run` 会生成计划文件，但不会调用 FFmpeg 导出视频。
运行以下命令查看参数：

```bash
python3 extract_clips.py --help
python3 clips.py --help
```

| 参数 | 默认值 | 说明 |
| `--timeline NAME` | `timestamps` | 选择时间戳文件族：`src`、`encoded`、`decoded`、`timestamps` |
| `--no-validate-mp4-frames` | 关闭 | 不将 CSV 行与 MP4 实际帧 PTS 匹配 |
| `--annotation-timebase NAME` | `raw-primary` | 标注时间基准：`raw-primary` 或 `aligned` |
| `--include-only-left` | 关闭 | 额外导出主视角的左眼裁剪视频 |
| `--include-only-left` | 命令行默认关闭；`instances.segments` 标注格式下自动开启 | 额外导出主视角的左眼裁剪视频 |
| `--simple-output-names` | 关闭 | 使用 `primary.mp4` 等简化名称 |
| `--make-grid` | 关闭 | 每个 section 生成 2×2 网格视频 |
| `--grid-cell-width N` | `960` | 网格单元宽度 |

### 1. 按标注导出片段

根据标注视频的不同，裁切原始视频有两种用法。两种方式都会使用四路原始视频作为输入，并根据时间戳完成四路时间对齐。

#### 1.1 对齐视频标注后，裁切原始视频

适用于先生成 `aligned/` 对齐视频、在对齐视频上进行标注，之后仍希望从原始四路视频中导出片段的情况。

此时标注中的秒数是相对于公共对齐区间的时间，因此使用 `--annotation-timebase aligned`：

```bash
python3 clips.py /data/session_001 \
  --annotation /data/session_001/annotation.json \
  --annotation-timebase aligned \
  --include-only-left \
  --reencode
```

这里的 `recording_dir` 应是包含原始四路 MP4 和 `_timestamps/` 的目录，不是 `aligned/` 目录。脚本会读取原始视频，并把对齐视频上的标注时间映射回原始视频后裁切。

#### 1.2 对原始视频标注后，裁切原始视频

适用于直接在原始 `recording_primary.mp4` 上进行标注的情况。默认时间基准就是 `raw-primary`，也可以显式写出：

```bash
python3 clips.py /data/session_001 \
  --annotation /data/session_001/annotation.json \
  --annotation-timebase raw-primary \
  --include-only-left \
  --reencode
```

如果不需要额外导出左眼裁剪视频，可以去掉 `--include-only-left`。不过，当 `annotation.json` 使用 `instances.segments` 格式时，脚本会自动启用左眼裁剪，即使没有这个参数。

```bash
python3 extract_clips.py /data/session_001 \
python3 clips.py /data/session_001 \
  --annotation /data/session_001/annotation.json \
  --output-dir /data/session_001/clips \
  --reencode
### 2. 导出完整对齐视频

```bash
python3 extract_clips.py /data/session_001 --align-full --reencode
python3 clips.py /data/session_001 --align-full --reencode
```

脚本会计算四路视频共有的 wall-clock 区间，并在 `aligned/` 下导出四路视频，同时生成：
### 3. 使用已对齐视频导出片段

```bash
python3 extract_clips.py /data/session_001/aligned \
python3 clips.py /data/session_001/aligned \
  --annotation /data/session_001/annotation.json
```

### 4. 导出左眼裁剪片段

```bash
python3 extract_clips.py /data/session_001 \
python3 clips.py /data/session_001 \
  --include-only-left \
  --reencode
