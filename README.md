# OW Chat Translator · 守望聊天翻译器

一个**完全离线**的韩中双向翻译工具，给在韩服打《守望先锋》的玩家用。

- 看懂队友在喊什么（韩→中）
- 把你想说的话翻成韩语（中→韩）
- 截图 OCR，热键一键翻译
- 本地模型，不联网、不注册、不上传任何数据

> 不注入游戏进程、不读内存、不改游戏文件——只处理你截图的那张图片。

---

## 功能

| 功能 | 说明 |
| --- | --- |
| 双向翻译 | 自动判断语向，也可手动锁定「韩→中 / 中→韩」 |
| 截图识别 | `Win+Shift+S` 截图后 `Ctrl+V`，自动 OCR + 翻译 |
| 全局热键 | 预设区域后按 `F8`，自动截图并翻译 |
| 框选区域 | 拖一次记住聊天框位置，之后免设置 |
| 离线运行 | Qwen2.5 + llama.cpp，全本地推理 |
| GPU 加速 | 检测到 NVIDIA 显卡自动走 CUDA，否则退回 CPU |

## 实测速度

| 环境 | 韩→中 | 中→韩 |
| --- | --- | --- |
| RTX 4060 Ti (CUDA · 7B) | 0.15–0.4 秒 | 0.3–0.5 秒 |
| 纯 CPU (7B) | 2–5 秒 | 2–5 秒 |

## 下载与安装

安装包约 5.06 GB，托管在网盘（GitHub Release 单文件上限 2 GB，放不下）：

| 网盘 | 链接 | 提取码 |
| --- | --- | --- |
| 夸克网盘 | https://pan.quark.cn/s/2478c647ca8d | `CPGt` |

下载后把三个分卷（`zip.001` / `zip.002` / `zip.003`）和 `合并解压.bat`
放进同一个文件夹，双击 `合并解压.bat`，程序会自动合并、解压并启动。

> 三个分卷缺一不可；合并后是 5.06 GB 的 zip，解开就是绿色版目录。

详细步骤见 [`docs/使用说明.txt`](docs/使用说明.txt)。

## 从源码运行

```bash
# 1. 准备 Python 3.12
python -m venv venv
venv\Scripts\python -m pip install rapidocr onnxruntime pillow

# 2. 下载模型（约 4.5 GB）
node scripts/download-model-shards.mjs

# 3. 下载 llama.cpp 运行时（CPU 版）
#    https://github.com/ggml-org/llama.cpp/releases
#    解压到 runtime/llama/

# 4. 运行
venv\Scripts\python app\ow_translate.py
```

## 构建绿色版

```bash
venv\Scripts\python -m PyInstaller --noconfirm --clean --windowed ^
  --name OWTranslator --collect-data rapidocr app\ow_translate.py
```

把 `runtime\` 和 `models\` 一起放进输出目录即可。

## 配置

同目录下的 `config.json`（运行后生成）：

```json
{
  "direction": "auto",
  "region": [0, 0, 800, 200],
  "hotkey": "F8",
  "model": "auto"
}
```

`hotkey` 支持 `F1`–`F10`；`model` 可选 `auto` / `7b` / `3b`。

## 第三方组件

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [Qwen2.5-7B-Instruct](https://github.com/QwenLM/Qwen2.5) | 翻译模型 | Apache-2.0 |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | 本地推理 | MIT |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | 韩语 OCR | Apache-2.0 |
| [PyInstaller](https://pyinstaller.org/) | 打包 | GPL-2.0 + 例外条款 |

详见 [`NOTICE.md`](NOTICE.md)。

## 免责声明

本工具为第三方辅助软件，**与暴雪娱乐无关**。
它不注入游戏进程、不修改游戏文件，但仍请自行评估在在线游戏中使用第三方工具的风险。
因使用本工具导致的任何后果由使用者自行承担。

## License

[MIT](LICENSE)
