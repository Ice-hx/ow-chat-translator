# -*- coding: utf-8 -*-
"""
守望聊天翻译器 v2（双向 · 全局热键 · CUDA 加速）
------------------------------------------------------
· OCR  ：RapidOCR 韩语模型（自动放大，解决小字号）
· 翻译  ：llama.cpp + Qwen2.5（优先 7B，有显卡走 CUDA）
· 方向  ：韩→中 / 中→韩，可自动判断
· 热键  ：F8 截取预设区域并翻译（可改）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import ttk

if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
    ROOT = APP_DIR
else:
    APP_DIR = Path(__file__).resolve().parent
    ROOT = APP_DIR.parent

CONFIG_PATH = ROOT / 'config.json'
PORT = 18080
BASE_URL = f'http://127.0.0.1:{PORT}'

KO2ZH = """你是《守望先锋》韩服聊天的翻译器，把韩语翻成简体中文。

固定译法（必须遵守）：
힐=治疗  궁=大招  겐지=源氏  겐지새끼=源氏那家伙
뒤=后面  조심=小心  제발=拜托   ㅅㅂ=操  ㅈㅈ=GG
죽었=死了  살려=救我  물러=后撤  가자=上  힐러=奶妈

规则：
1. 只输出中文译文，不解释、不加引号、不音译
2. 保留原句语气：骂人就翻译成骂人，催促就翻成催促
3. 原文很短就直接短译，不要扩写成完整句子
4. 不确定的英雄名按常见译名处理"""

ZH2KO = """你是《守望先锋》韩服聊天的翻译器，把简体中文翻成自然的韩语口语。

固定译法（必须遵守）：
治疗=힐  大招=궁  源氏=겐지  后面=뒤  小心=조심
拜托=제발  操=ㅅㅂ  GG=ㅈㅈ  救我=살려  后撤=물러  上=가자
没血=피 없어  血量=체력  血=피  别送了=그만 죽어  送=죽다
集合=모여  跟我来=따라와  打得好=잘했어  谢谢=고마워

规则：
1. 只输出韩语译文，不解释、不加引号、不加路径或标签
2. 用游戏里真实会说的短句，不要书面语
3. 保留语气强度：骂人就翻成骂人，催促就翻成催促
4. 韩语要自然，不要逐字直译"""

SHOTS_KO2ZH = [
    ('힐 좀 줘', '给点治疗'),
    ('궁 써라 지금', '现在放大招'),
    ('뒤에 겐지 있어', '后面有源氏'),
    ('ㅅㅂ 또 죽었네', '操，又死了'),
    ('제발 조심해', '拜托小心点'),
]

SHOTS_ZH2KO = [
    ('给点治疗', '힐 좀 줘'),
    ('现在放大招', '지금 궁 써'),
    ('后面有源氏', '뒤에 겐지 있어'),
    ('操，又死了', 'ㅅㅂ 또 죽었네'),
    ('后撤', '물러'),
]

HANGUL = re.compile(r'[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]')

# 中→韩 的兜底短语表：模型偶尔吐出非韩文（实测出现过 "hế수"），命中就整句替换
ZH_FALLBACK = {
    '别送了': '그만 죽어', '别送': '그만 죽어', '别送了。': '그만 죽어',
    '集合': '모여', '跟我来': '따라와', '打得好': '잘했어',
    '谢谢': '고마워', '对不起': '미안', '上': '가자', '退': '물러',
    '给点治疗': '힐 좀 줘', '放大招': '궁 써', '开大': '궁 써',
    '现在开大': '지금 궁 써', '现在放大招': '지금 궁 써',
    '后面有人': '뒤에 있어', '小心': '조심해', '救我': '살려줘',
}


def clean_output(text: str) -> str:
    """模型偶尔会带路径前缀、引号或“译文：”，统一清掉。"""
    t = (text or '').strip()
    t = re.sub(r'^/[A-Za-z0-9_]+/\s*', '', t)
    t = re.sub(r'^(译文|翻译|韩语|中文|结果)\s*[:：]\s*', '', t)
    t = t.strip().strip('"').strip('“').strip('”').strip("'").strip()
    return t


# ---------------------------------------------------------------- 配置
def load_config() -> dict:
    cfg = {
        'direction': 'auto',      # auto | ko2zh | zh2ko
        'region': None,           # [left, top, right, bottom]
        'hotkey': 'F8',
        'model': 'auto',          # auto | 7b | 3b
    }
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


# ---------------------------------------------------------------- 翻译后端
class Translator:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.ready = False
        self.last_error = ''
        self.mode = ''
        self._lock = threading.Lock()

    # -- 选择运行时与模型 ------------------------------------------------
    def _pick_runtime(self) -> tuple[Path, bool]:
        cuda = ROOT / 'runtime' / 'llama-cuda'
        cpu = ROOT / 'runtime' / 'llama'
        if (cuda / 'llama-server.exe').exists():
            return cuda, True
        return cpu, False

    def _pick_model(self) -> Path | None:
        models = ROOT / 'models'
        prefer = self.cfg.get('model', 'auto')
        # 7B 的 Q4_K_M 在 ModelScope 上是分片的，传第一个分片即可，llama.cpp 会自动找后续分片
        b7 = models / 'qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf'
        if not b7.exists():
            b7 = models / 'qwen2.5-7b-instruct-q4_k_m.gguf'
        b3 = models / 'qwen2.5-3b-instruct-q4_k_m.gguf'
        if prefer == '7b' and b7.exists():
            return b7
        if prefer == '3b' and b3.exists():
            return b3
        if b7.exists():
            return b7
        if b3.exists():
            return b3
        return None

    # -- 启动 ------------------------------------------------------------
    def ensure_server(self, on_log=None) -> bool:
        with self._lock:
            if self.ready and self._ping():
                return True
            runtime, is_cuda = self._pick_runtime()
            exe = runtime / 'llama-server.exe'
            model = self._pick_model()
            if not exe.exists():
                self.last_error = f'缺少运行时: {exe}'
                return False
            if model is None:
                self.last_error = '缺少模型文件（models 目录）'
                return False
            if on_log:
                on_log(f'正在启动 {"CUDA" if is_cuda else "CPU"} 模型…')
            cpu = max(4, (os.cpu_count() or 8) // 2)
            args = [
                str(exe), '-m', str(model), '-c', '2048', '-t', str(cpu),
                '--host', '127.0.0.1', '--port', str(PORT), '--no-warmup',
            ]
            if is_cuda:
                args += ['-ngl', '99']
            creation = 0x08000000 if os.name == 'nt' else 0
            try:
                self.proc = subprocess.Popen(
                    args, cwd=str(runtime),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=creation,
                )
            except OSError as exc:
                self.last_error = f'启动失败: {exc}'
                return False

            deadline = time.time() + 180
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    self.last_error = '模型进程退出了（可能显存不足，可切回 3B 或 CPU）'
                    return False
                if self._ping():
                    self.ready = True
                    self.mode = ('CUDA' if is_cuda else 'CPU') + ' · ' + model.stem.split('-')[1].upper()
                    if on_log:
                        on_log(f'就绪（{self.mode}）')
                    return True
                time.sleep(0.5)
            self.last_error = '模型加载超时'
            return False

    def _ping(self) -> bool:
        try:
            with urllib.request.urlopen(f'{BASE_URL}/health', timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    # -- 翻译 ------------------------------------------------------------
    def translate(self, text: str, direction: str, timeout: float = 90.0) -> str:
        # 中→韩：完全命中短语表时直接用词典，避免小模型胡编
        if direction == 'zh2ko':
            exact = ZH_FALLBACK.get(text.strip().rstrip('。！!？?'))
            if exact:
                return exact
        if direction == 'ko2zh':
            system, shots = KO2ZH, SHOTS_KO2ZH
        else:
            system, shots = ZH2KO, SHOTS_ZH2KO
        messages = [{'role': 'system', 'content': system}]
        for src, dst in shots:
            messages.append({'role': 'user', 'content': src})
            messages.append({'role': 'assistant', 'content': dst})
        messages.append({'role': 'user', 'content': text})

        payload = {
            'messages': messages,
            'temperature': 0.1,
            'max_tokens': 96,
            'stream': False,
        }
        req = urllib.request.Request(
            f'{BASE_URL}/v1/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            out = clean_output(data['choices'][0]['message']['content'])
            # 中→韩：如果输出里一个韩文字符都没有，说明模型跑偏了，退回短语表
            if direction == 'zh2ko' and not HANGUL.search(out):
                alt = self._zh_fallback(text)
                if alt:
                    out = alt
            return out
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'翻译请求失败: {exc}') from exc

    @staticmethod
    def _zh_fallback(text: str) -> str:
        t = text.strip().rstrip('。！!？?')
        if t in ZH_FALLBACK:
            return ZH_FALLBACK[t]
        for key in sorted(ZH_FALLBACK, key=len, reverse=True):
            if key in t:
                return ZH_FALLBACK[key]
        return ''

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------- OCR
class Ocr:
    def __init__(self) -> None:
        self.engine = None
        self.error = ''
        self._lock = threading.Lock()

    def ensure(self) -> bool:
        with self._lock:
            if self.engine is not None:
                return True
            try:
                from rapidocr import RapidOCR, EngineType, ModelType, OCRVersion

                self.engine = RapidOCR(params={
                    'Rec.engine_type': EngineType.ONNXRUNTIME,
                    'Rec.lang_type': 'korean',
                    'Rec.model_type': ModelType.MOBILE,
                    'Rec.ocr_version': OCRVersion.PPOCRV4,
                })
                return True
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                return False

    def read(self, image) -> str:
        if not self.ensure():
            raise RuntimeError(self.error or 'OCR 初始化失败')
        import numpy as np
        from PIL import Image

        scale = 1
        if image.height < 120:
            scale = 4
        elif image.height < 240:
            scale = 3
        elif image.height < 480:
            scale = 2
        while image.width * scale > 4000 and scale > 1:
            scale -= 1
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)

        result = self.engine(np.array(image.convert('RGB')))
        txts = getattr(result, 'txts', None) or []
        return '\n'.join(t.strip() for t in txts if t and t.strip())


# ---------------------------------------------------------------- 区域框选
class RegionPicker(tk.Toplevel):
    def __init__(self, master: tk.Tk, on_done) -> None:
        super().__init__(master)
        self.on_done = on_done
        self.start = None
        self.rect = None
        self.attributes('-fullscreen', True)
        self.attributes('-alpha', 0.35)
        self.attributes('-topmost', True)
        self.configure(bg='black', cursor='crosshair')
        self.canvas = tk.Canvas(self, bg='black', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.create_text(
            40, 40, anchor='nw', fill='white', font=('Microsoft YaHei UI', 16),
            text='拖动鼠标框选聊天区域 · Esc 取消',
        )
        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._drag)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self.bind('<Escape>', lambda e: self._cancel())

    def _press(self, e) -> None:
        self.start = (e.x_root, e.y_root)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline='#4da3ff', width=3)

    def _drag(self, e) -> None:
        if self.rect and self.start:
            x0 = self.start[0] - self.winfo_rootx()
            y0 = self.start[1] - self.winfo_rooty()
            self.canvas.coords(self.rect, x0, y0, e.x, e.y)

    def _release(self, e) -> None:
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x_root, e.y_root
        box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        self.destroy()
        if box[2] - box[0] > 20 and box[3] - box[1] > 20:
            self.on_done(box)

    def _cancel(self) -> None:
        self.destroy()


# ---------------------------------------------------------------- 全局热键
class Hotkey(threading.Thread):
    """用 Win32 RegisterHotKey 注册全局热键，不依赖第三方库。"""

    MODS = {'F1': 0x70, 'F2': 0x71, 'F3': 0x72, 'F4': 0x73, 'F5': 0x74,
            'F6': 0x75, 'F7': 0x76, 'F8': 0x77, 'F9': 0x78, 'F10': 0x79}

    def __init__(self, key: str, callback) -> None:
        super().__init__(daemon=True)
        self.vk = self.MODS.get(key.upper(), 0x77)
        self.callback = callback
        self.ok = False

    def run(self) -> None:
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, 0, self.vk):
            return
        self.ok = True
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:      # WM_HOTKEY
                    try:
                        self.callback()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)


# ---------------------------------------------------------------- 界面
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.title('OW 聊天翻译 · 双向')
        self.geometry('500x620+40+60')
        self.minsize(420, 480)
        self.attributes('-topmost', True)
        self.configure(bg='#1b1d22')

        self.translator = Translator(self.cfg)
        self.ocr = Ocr()
        self.events: queue.Queue = queue.Queue()
        self._busy = False
        self._last_dir = 'ko2zh'

        self._build_ui()
        self.after(100, self._drain)
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        threading.Thread(target=self._warm_up, daemon=True).start()

        hk = Hotkey(self.cfg.get('hotkey', 'F8'), lambda: self.events.put(('hotkey', None)))
        hk.start()
        self.after(1200, lambda: self._hotkey_status(hk.ok))

    # -- UI ---------------------------------------------------------------
    def _build_ui(self) -> None:
        ui = ('Microsoft YaHei UI', 10)
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TFrame', background='#1b1d22')
        style.configure('TLabel', background='#1b1d22', foreground='#c9ccd4', font=ui)
        style.configure('TButton', font=ui)
        style.configure('TRadiobutton', background='#1b1d22', foreground='#c9ccd4', font=ui)

        root = ttk.Frame(self, padding=12)
        root.pack(fill='both', expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)
        root.rowconfigure(7, weight=2)

        self.status = ttk.Label(root, text='初始化…', foreground='#8ab4f8')
        self.status.grid(row=0, column=0, sticky='w')

        dirs = ttk.Frame(root)
        dirs.grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.dir_var = tk.StringVar(value=self.cfg.get('direction', 'auto'))
        for text, val in (('自动', 'auto'), ('韩→中', 'ko2zh'), ('中→韩', 'zh2ko')):
            ttk.Radiobutton(dirs, text=text, value=val, variable=self.dir_var,
                            command=self._save_dir).pack(side='left', padx=(0, 8))

        ttk.Label(root, text='韩文 / 中文（Ctrl+V 粘贴截图）').grid(row=3, column=0, sticky='w', pady=(8, 4))

        self.input = tk.Text(root, height=4, wrap='word', bg='#252830', fg='#e8eaed',
                             insertbackground='#e8eaed', relief='flat',
                             font=('Malgun Gothic', 12), padx=8, pady=6)
        self.input.grid(row=4, column=0, sticky='nsew')
        self.input.bind('<Control-Return>', lambda e: self.do_translate())

        bar = ttk.Frame(root)
        bar.grid(row=5, column=0, sticky='ew', pady=8)
        ttk.Button(bar, text='翻译 (Ctrl+Enter)', command=self.do_translate).pack(side='left')
        ttk.Button(bar, text='粘贴截图', command=self.do_paste_image).pack(side='left', padx=6)
        ttk.Button(bar, text='复制译文', command=self.copy_result).pack(side='left')
        ttk.Button(bar, text='清空', command=self.clear).pack(side='right')

        ttk.Label(root, text='译文').grid(row=6, column=0, sticky='w', pady=(4, 4))
        self.output = tk.Text(root, height=5, wrap='word', bg='#252830', fg='#9fe6a0',
                              relief='flat', font=('Microsoft YaHei UI', 12), padx=8, pady=6)
        self.output.grid(row=7, column=0, sticky='nsew')

        zone = ttk.Frame(root)
        zone.grid(row=8, column=0, sticky='ew', pady=(10, 0))
        ttk.Button(zone, text='框选聊天区域', command=self.pick_region).pack(side='left')
        self.region_label = ttk.Label(zone, text=self._region_text(), foreground='#7d828c')
        self.region_label.pack(side='left', padx=8)
        ttk.Label(zone, text=f'热键 {self.cfg.get("hotkey", "F8")}', foreground='#7d828c').pack(side='right')

        ttk.Label(
            root,
            text='游戏请用「无边框窗口」；本工具不注入游戏进程，不会被反作弊波及。',
            foreground='#7d828c', wraplength=460, justify='left',
        ).grid(row=9, column=0, sticky='w', pady=(8, 0))

    # -- 状态 -------------------------------------------------------------
    def log(self, msg: str) -> None:
        self.events.put(('status', msg))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'status':
                    self.status.configure(text=payload, foreground='#8ab4f8')
                elif kind == 'input':
                    self.input.delete('1.0', 'end')
                    self.input.insert('1.0', payload)
                elif kind == 'output':
                    self.output.delete('1.0', 'end')
                    self.output.insert('1.0', payload)
                elif kind == 'error':
                    self.status.configure(text=payload, foreground='#ff8a80')
                elif kind == 'done':
                    self.status.configure(text=payload, foreground='#8ab4f8')
                elif kind == 'hotkey':
                    self._hotkey_action()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _hotkey_status(self, ok: bool) -> None:
        if not ok:
            self.status.configure(text='热键注册失败（可能被占用），请用按钮操作', foreground='#ffb74d')

    def _region_text(self) -> str:
        r = self.cfg.get('region')
        return '未设置' if not r else f'{r[2] - r[0]}×{r[3] - r[1]}'

    def _save_dir(self) -> None:
        self.cfg['direction'] = self.dir_var.get()
        save_config(self.cfg)

    # -- 预热 -------------------------------------------------------------
    def _warm_up(self) -> None:
        self.events.put(('status', '正在加载 OCR…'))
        self.ocr.ensure()
        self.events.put(('status', '正在启动本地模型…'))
        if self.translator.ensure_server(lambda m: self.events.put(('status', m))):
            self.events.put(('done', f'就绪 · {self.translator.mode} · 按 {self.cfg.get("hotkey", "F8")} 截图翻译'))
        else:
            self.events.put(('error', self.translator.last_error or '模型启动失败'))

    # -- 区域 -------------------------------------------------------------
    def pick_region(self) -> None:
        self.withdraw()
        self.after(200, lambda: RegionPicker(self, self._region_chosen))

    def _region_chosen(self, box: list[int]) -> None:
        self.deiconify()
        self.cfg['region'] = box
        save_config(self.cfg)
        self.region_label.configure(text=self._region_text())
        self.status.configure(text='区域已保存', foreground='#8ab4f8')

    # -- 热键触发 ---------------------------------------------------------
    def _hotkey_action(self) -> None:
        region = self.cfg.get('region')
        if not region:
            self.status.configure(text='先点「框选聊天区域」设置一次', foreground='#ffb74d')
            return
        self.withdraw()
        self.after(120, lambda: self._capture_and_translate(region))

    def _capture_and_translate(self, region) -> None:
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab(bbox=tuple(region), all_screens=True)
        except Exception as exc:  # noqa: BLE001
            self.deiconify()
            self.status.configure(text=f'截图失败: {exc}', foreground='#ff8a80')
            return
        self.deiconify()
        self._run_async(self._job_pipeline, img)

    # -- 主流程 -----------------------------------------------------------
    def do_paste_image(self) -> None:
        try:
            from PIL import ImageGrab

            img = ImageGrab.grabclipboard()
        except Exception as exc:  # noqa: BLE001
            self.status.configure(text=f'读取剪贴板失败: {exc}', foreground='#ff8a80')
            return
        if img is None or not hasattr(img, 'size'):
            self.status.configure(text='剪贴板里没有图片，先 Win+Shift+S 截图', foreground='#ffb74d')
            return
        self._run_async(self._job_pipeline, img)

    def _job_pipeline(self, img) -> None:
        self.events.put(('status', '识别中…'))
        text = self.ocr.read(img)
        if not text:
            raise RuntimeError('没识别出文字，试着放大截图或换个区域')
        self.events.put(('input', text))
        self._job_translate(text, self._decide_direction(text))

    def do_translate(self) -> None:
        text = self.input.get('1.0', 'end').strip()
        if not text:
            self.status.configure(text='先粘贴截图或输入文字', foreground='#ffb74d')
            return
        self._run_async(self._job_translate, text, self._decide_direction(text))

    def _decide_direction(self, text: str) -> str:
        chosen = self.dir_var.get()
        if chosen in ('ko2zh', 'zh2ko'):
            self._last_dir = chosen
            return chosen
        direction = 'ko2zh' if HANGUL.search(text) else 'zh2ko'
        self._last_dir = direction
        return direction

    def _job_translate(self, text: str, direction: str) -> None:
        if not self.translator.ensure_server(lambda m: self.events.put(('status', m))):
            raise RuntimeError(self.translator.last_error or '模型未就绪')
        self.events.put(('status', f'翻译中（{"韩→中" if direction == "ko2zh" else "中→韩"}）…'))
        t0 = time.time()
        out = self.translator.translate(text, direction)
        self.events.put(('output', out))
        self.events.put(('done', f'完成 · {time.time() - t0:.1f}s'))

    def _run_async(self, fn, *args) -> None:
        if self._busy:
            self.status.configure(text='正在处理上一条，稍等', foreground='#ffb74d')
            return
        self._busy = True

        def runner():
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                self.events.put(('error', str(exc)))
            finally:
                self._busy = False

        threading.Thread(target=runner, daemon=True).start()

    # -- 其他 -------------------------------------------------------------
    def copy_result(self) -> None:
        text = self.output.get('1.0', 'end').strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status.configure(text='已复制', foreground='#8ab4f8')

    def clear(self) -> None:
        self.input.delete('1.0', 'end')
        self.output.delete('1.0', 'end')
        self.status.configure(text='就绪', foreground='#8ab4f8')

    def _on_close(self) -> None:
        self.translator.stop()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
