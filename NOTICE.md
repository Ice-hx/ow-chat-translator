# 第三方组件声明

本项目打包分发的「绿色版」中，包含或依赖以下第三方组件。
各组件的版权与许可归其各自作者所有。

---

## 1. Qwen2.5-7B-Instruct（翻译模型）

- 作者：阿里云通义千问团队（Alibaba Qwen）
- 项目主页：https://github.com/QwenLM/Qwen2.5
- 许可：**Apache License 2.0**
- 用途：本项目的核心翻译模型（GGUF 量化版）
- 说明：模型权重未包含在本仓库中，由用户通过 `scripts/download-model-shards.mjs`
  从 ModelScope 官方镜像下载。

## 2. llama.cpp（推理引擎）

- 作者：ggml-org 及贡献者
- 项目主页：https://github.com/ggml-org/llama.cpp
- 许可：**MIT License**
- 用途：本地 GGUF 模型推理，提供 `llama-server`。

## 3. RapidOCR（韩语文字识别）

- 作者：RapidAI
- 项目主页：https://github.com/RapidAI/RapidOCR
- 许可：**Apache License 2.0**
- 用途：截图中的韩语 OCR（PP-OCRv4 korean 识别模型）。

## 4. PyInstaller（打包工具）

- 项目主页：https://pyinstaller.org/
- 许可：**GPL-2.0 with a special exception**
- 说明：仅用于打包，其例外条款允许分发打包后的程序。

## 5. 其他 Python 依赖

| 包 | 许可 |
| --- | --- |
| Pillow | HPND |
| onnxruntime | MIT |
| numpy | BSD-3-Clause |

---

## 模型权重再分发说明

Qwen2.5 采用 Apache-2.0 许可，允许再分发，但需保留原始许可与版权声明。
本项目**不在仓库中保存模型权重**，仅提供下载脚本，以减小仓库体积并尊重上游分发渠道。

## 商标说明

《守望先锋》(Overwatch) 是暴雪娱乐（Blizzard Entertainment）的注册商标。
本项目为第三方工具，与暴雪娱乐无任何关联，也未获得其背书。
