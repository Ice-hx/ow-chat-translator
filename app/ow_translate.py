# -*- coding: utf-8 -*-
"""
守望聊天翻译器 v3（多语种 · 双向 · 全局热键 · CUDA 加速）
------------------------------------------------------
支持：韩语 / 英语 / 日语 ↔ 中文
· OCR  ：RapidOCR，按语言切换识别模型（ko / en / ja / 中日英混合）
· 翻译  ：llama.cpp + Qwen2.5（优先 7B，有显卡走 CUDA）
· 方向  ：源语言可自动检测；中文输入时可指定译成韩/英/日
· 热键  ：F8 截取预设区域并翻译
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

# ---------------------------------------------------------------- 语言
LANGS = {'zh': '中文', 'ko': '韩语', 'en': '英语', 'ja': '日语'}
SRC_CHOICES = [('自动', 'auto'), ('中文', 'zh'), ('韩语', 'ko'), ('英语', 'en'), ('日语', 'ja')]
TGT_CHOICES = [('中文', 'zh'), ('韩语', 'ko'), ('英语', 'en'), ('日语', 'ja')]

HANGUL = re.compile(r'[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]')
KANA = re.compile(r'[\u3040-\u30FF]')
CJK = re.compile(r'[\u4E00-\u9FFF]')
LATIN = re.compile(r'[A-Za-z]')


def detect_lang(text: str) -> str:
    """按字符集猜源语言。中文/日文都有汉字，所以先看假名再看韩文。"""
    if HANGUL.search(text):
        return 'ko'
    if KANA.search(text):
        return 'ja'
    if CJK.search(text):
        return 'zh'
    if LATIN.search(text):
        return 'en'
    return 'zh'


def _hangul_ratio(text: str) -> float:
    """非空白字符里韩文占多大比例。用于 auto 模式排除误识别的乱码。"""
    total = sum(1 for c in text if not c.isspace())
    if not total:
        return 0.0
    return len(HANGUL.findall(text)) / total


# ---------------------------------------------------------------- 提示词
RULE_TAIL = """
规则：
1. 只输出译文，不解释、不加引号、不加语言标签
2. 保留原句语气：骂人就翻译成骂人，催促就翻成催促
3. 原文很短就直接短译，不要扩写成完整句子
4. 英雄名用中文常见译名（源氏、半藏、天使、禅雅塔、查莉娅…）"""

PROMPTS: dict[tuple[str, str], tuple[str, list[tuple[str, str]]]] = {
    ('ko', 'zh'): (
        """你是《守望先锋》韩服聊天的翻译器，把韩语翻成简体中文。

固定译法（必须遵守）：
힐=治疗  궁=大招  겐지=源氏  뒤=后面  조심=小心
제발=拜托  ㅅㅂ=操  ㅈㅈ=GG  살려=救我  물러=后撤  가자=上
""" + RULE_TAIL,
        [
            ('힐 좀 줘', '给点治疗'),
            ('궁 써라 지금', '现在放大招'),
            ('뒤에 겐지 있어', '后面有源氏'),
            ('ㅅㅂ 또 죽었네', '操，又死了'),
            ('제발 조심해', '拜托小心点'),
        ],
    ),
    ('en', 'zh'): (
        """你是《守望先锋》聊天翻译器，把英语游戏聊天翻成简体中文。

固定译法（必须遵守）：
heal / heals = 治疗   ult / ultimate = 大招   Genji = 源氏
behind = 后面   careful = 小心   push = 上   retreat / back = 后撤
gg = GG   no heals = 没治疗   feed / feeding = 送

缩写与黑话（必须按这个译）：
wp = 打得漂亮   ns / nice shot = 好枪   ez = 轻松
diff = 差距（tank diff = 坦克差距，heal diff = 治疗差距）
c9 = 重大失误   peel = 掩护   on me = 跟我
one hp = 一枪血   rez = 复活   shatter = 裂地猛击
grav = 重力喷涌   nano = 纳米激素   blade = 拔刀
throw = 演 / 故意送   afk = 挂机   brb = 马上回来
stagger = 逐个击杀 / 拖延   contest = 占点   smurf = 小号
shotcall = 指挥   poke = 消耗   dive = 突进   bait = 骗
incoming = 要来了
""" + RULE_TAIL,
        [
            ('heal me', '给我治疗'),
            ('ult now', '现在放大招'),
            ('Genji behind', '源氏在后面'),
            ('ns', '好枪'),
            ('peel for me', '掩护我'),
            ('tank diff', '坦克差距'),
            ('throw', '演'),
        ],
    ),
    ('ja', 'zh'): (
        """你是《守望先锋》聊天翻译器，把日语游戏聊天翻成简体中文。

固定译法（必须遵守）：
ヒール = 治疗   ウルト / アルティメット = 大招   ゲンジ = 源氏
後ろ = 后面   注意 = 小心   押し = 上   撤退 = 后撤
gg = GG   回復 = 治疗   無理 = 打不过
""" + RULE_TAIL,
        [
            ('ヒールください', '请给治疗'),
            ('ウルト使って', '放大招'),
            ('後ろにゲンジ', '后面有源氏'),
            ('押しに行くぞ', '上'),
            ('gg', 'GG'),
        ],
    ),
    ('zh', 'ko'): (
        """你是《守望先锋》韩服聊天的翻译器，把简体中文翻成自然的韩语口语。

固定译法（必须遵守）：
治疗=힐  大招=궁  源氏=겐지  后面=뒤  小心=조심  拜托=제발
操=ㅅㅂ  GG=ㅈㅈ  救我=살려  后撤=물러  上=가자
没血=피 없어  血量=체력  别送了=그만 죽어  集合=모여  打得好=잘했어
""" + RULE_TAIL.replace('英雄名用中文常见译名（源氏、半藏、天使、禅雅塔、查莉娅…）',
                         '英雄名用韩语常见写法（겐지、한조、메르시、젠야타、자리야…）'),
        [
            ('给点治疗', '힐 좀 줘'),
            ('现在放大招', '지금 궁 써'),
            ('后面有源氏', '뒤에 겐지 있어'),
            ('操，又死了', 'ㅅㅂ 또 죽었네'),
            ('后撤', '물러'),
        ],
    ),
    ('zh', 'en'): (
        """你是《守望先锋》聊天的翻译器，把简体中文翻成地道的英语游戏口语。

固定译法（必须遵守）：
治疗=heals  大招=ult  源氏=Genji  后面=behind  小心=careful
上=push  后撤=retreat  救我=help me  别送了=stop feeding
没血=I'm low  血量=HP  残血=low HP
""" + RULE_TAIL.replace('英雄名用中文常见译名（源氏、半藏、天使、禅雅塔、查莉娅…）',
                         '英雄名用英文名（Genji、Hanzo、Mercy、Zenyatta、Zarya…）'),
        [
            ('给点治疗', 'heals please'),
            ('现在放大招', 'ult now'),
            ('后面有源氏', 'Genji behind'),
            ('操，又死了', 'ffs, dead again'),
            ('后撤', 'fall back'),
        ],
    ),
    ('zh', 'ja'): (
        """你是《守望先锋》聊天的翻译器，把简体中文翻成自然的日语游戏口语。

固定译法（必须遵守）：
治疗=ヒール  大招=ウルト  源氏=ゲンジ  后面=後ろ  小心=注意
上=押し  后撤=撤退  救我=助けて  别送了=死にすぎ
""" + RULE_TAIL.replace('英雄名用中文常见译名（源氏、半藏、天使、禅雅塔、查莉娅…）',
                         '英雄名用日语片假名写法（ゲンジ、ハンゾー、マーシー、ゼニヤッタ、ザリア…）'),
        [
            ('给点治疗', 'ヒールください'),
            ('现在放大招', 'ウルト使って'),
            ('后面有源氏', '後ろにゲンジ'),
            ('操，又死了', 'くそ、また死んだ'),
            ('后撤', '撤退'),
        ],
    ),
}

# 中→韩 的兜底短语表：模型偶尔吐出非韩文（实测出现过 "hế수"），命中就整句替换
ZH_FALLBACK = {
    '别送了': '그만 죽어', '别送': '그만 죽어',
    '集合': '모여', '跟我来': '따라와', '打得好': '잘했어',
    '谢谢': '고마워', '对不起': '미안', '上': '가자', '退': '물러',
    '给点治疗': '힐 좀 줘', '放大招': '궁 써', '开大': '궁 써',
    '现在开大': '지금 궁 써', '现在放大招': '지금 궁 써',
    '后面有人': '뒤에 있어', '小心': '조심해', '救我': '살려줘',
}

# 英→中 的缩写/黑话兜底：短句整句命中就直接返回，别让模型猜
EN_FALLBACK = {
    'gg': 'GG', 'wp': '打得漂亮', 'gg wp': 'GG，打得漂亮', 'ez': '轻松',
    'ns': '好枪', 'nice shot': '好枪', 'diff': '差距',
    'tank diff': '坦克差距', 'heal diff': '治疗差距', 'dps diff': '输出差距',
    'c9': '重大失误', 'peel': '掩护', 'peel for me': '掩护我',
    'on me': '跟我', 'one hp': '一枪血', '1 hp': '一枪血',
    'rez': '复活', 'rez me': '复活我', 'shatter': '裂地猛击',
    'grav': '重力喷涌', 'nano': '纳米激素', 'blade': '拔刀',
    'throw': '演', 'throwing': '在演', 'afk': '挂机', 'brb': '马上回来',
    'stagger': '逐个击杀', 'contest': '占点', 'contest point': '占点',
    'smurf': '小号', 'ffs': '靠', 'omg': '天啊', 'push': '上',
    'fall back': '后撤', 'regroup': '集合', 'group up': '集合',
    'ult': '大招', 'ult economy': '大招运营',
    'shotcall': '指挥', 'shotcalling': '指挥',
    'incoming': '要来了', 'nano blade incoming': '纳米激素拔刀要来了',
    'nano blade': '纳米拔刀', 'blade incoming': '拔刀要来了',
}


def _norm(text: str) -> str:
    return text.strip().lower().rstrip('。！!？?.')


def clean_output(text: str) -> str:
    """模型偶尔会带路径前缀、引号或“译文：”，统一清掉。"""
    t = (text or '').strip()
    t = re.sub(r'^/[A-Za-z0-9_]+/\s*', '', t)
    t = re.sub(r'^(译文|翻译|韩语|中文|英语|日语|结果)\s*[:：]\s*', '', t)
    t = t.strip().strip('"').strip('“').strip('”').strip("'").strip()
    return t


# ---------------------------------------------------------------- 配置
def load_config() -> dict:
    cfg = {
        'src': 'auto',            # auto | zh | ko | en | ja
        'tgt': 'zh',              # zh | ko | en | ja
        'zh_target': 'ko',        # 输入是中文时，默认译成哪种语言
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

    def _pick_runtime(self) -> tuple[Path, bool]:
        cuda = ROOT / 'runtime' / 'llama-cuda'
        cpu = ROOT / 'runtime' / 'llama'
        if (cuda / 'llama-server.exe').exists():
            return cuda, True
        return cpu, False

    def _pick_model(self) -> Path | None:
        models = ROOT / 'models'
        prefer = self.cfg.get('model', 'auto')
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

    def translate(self, text: str, src: str, tgt: str, timeout: float = 90.0) -> str:
        # 中→韩：完全命中短语表时直接用词典，避免小模型胡编
        if (src, tgt) == ('zh', 'ko'):
            exact = ZH_FALLBACK.get(text.strip().rstrip('。！!？?'))
            if exact:
                return exact
        if (src, tgt) == ('en', 'zh'):
            exact = EN_FALLBACK.get(_norm(text))
            if exact:
                return exact

        system, shots = PROMPTS.get((src, tgt), ('', []))
        if not system:
            system = f'把下面的{LANGS.get(src, src)}内容翻译成{LANGS.get(tgt, tgt)}，只输出译文。'

        messages = [{'role': 'system', 'content': system}]
        for a, b in shots:
            messages.append({'role': 'user', 'content': a})
            messages.append({'role': 'assistant', 'content': b})
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
            # 中→韩：输出里没有韩文，说明跑偏了，退回短语表
            if (src, tgt) == ('zh', 'ko') and not HANGUL.search(out):
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
    """按语言懒加载多个识别模型；auto 模式先试韩语，再试中日英混合。"""

    def __init__(self) -> None:
        self.engines: dict[str, object] = {}
        self.error = ''
        self._lock = threading.Lock()

    def _build(self, lang: str):
        from rapidocr import RapidOCR, EngineType, ModelType, OCRVersion

        if lang == 'ko':
            params = dict(lang='korean', version=OCRVersion.PPOCRV4)
        elif lang == 'en':
            params = dict(lang='en', version=OCRVersion.PPOCRV4)
        elif lang == 'ja':
            params = dict(lang='japan', version=OCRVersion.PPOCRV4)
        else:                      # 中日英混合（自动模式的兜底）
            params = dict(lang='ch', version=OCRVersion.PPOCRV5)

        return RapidOCR(params={
            'Rec.engine_type': EngineType.ONNXRUNTIME,
            'Rec.lang_type': params['lang'],
            'Rec.model_type': ModelType.MOBILE,
            'Rec.ocr_version': params['version'],
        })

    def engine(self, lang: str):
        with self._lock:
            if lang in self.engines:
                return self.engines[lang]
            try:
                eng = self._build(lang)
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                raise RuntimeError(self.error) from exc
            self.engines[lang] = eng
            return eng

    def warm_up(self) -> None:
        try:
            self.engine('ko')
        except Exception:
            pass

    def _run(self, image, lang: str) -> str:
        import numpy as np

        eng = self.engine(lang)
        result = eng(np.array(image.convert('RGB')))
        txts = getattr(result, 'txts', None) or []
        return '\n'.join(t.strip() for t in txts if t and t.strip())

    def read(self, image, lang: str = 'auto') -> str:
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

        if lang != 'auto':
            return self._run(image, lang)

        ko_text = ''
        try:
            ko_text = self._run(image, 'ko')
        except Exception:
            pass
        # 只看"有没有一个韩文字"不够：日文常被误认成含假韩文的乱码，
        # 所以要求韩文字符占比足够高才认作韩语。
        if ko_text and _hangul_ratio(ko_text) >= 0.4:
            return ko_text

        mixed = ''
        try:
            mixed = self._run(image, 'cjk')
        except Exception:
            pass
        return mixed or ko_text


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
        self.title('OW 聊天翻译 · 韩 / 英 / 日')
        self.geometry('520x660+40+60')
        self.minsize(440, 520)
        self.attributes('-topmost', True)
        self.configure(bg='#1b1d22')

        self.translator = Translator(self.cfg)
        self.ocr = Ocr()
        self.events: queue.Queue = queue.Queue()
        self._busy = False

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
        style.configure('TCombobox', font=ui)

        root = ttk.Frame(self, padding=12)
        root.pack(fill='both', expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)
        root.rowconfigure(8, weight=2)

        self.status = ttk.Label(root, text='初始化…', foreground='#8ab4f8')
        self.status.grid(row=0, column=0, sticky='w')

        langs = ttk.Frame(root)
        langs.grid(row=1, column=0, sticky='w', pady=(8, 0))

        ttk.Label(langs, text='源语言').pack(side='left')
        self.src_var = tk.StringVar(value=LANGS.get(self.cfg.get('src', 'auto'), '自动')
                                    if self.cfg.get('src') != 'auto' else '自动')
        self.src_box = ttk.Combobox(langs, textvariable=self.src_var, state='readonly',
                                    values=[t for t, _ in SRC_CHOICES], width=6)
        self.src_box.pack(side='left', padx=(6, 14))
        self.src_box.bind('<<ComboboxSelected>>', lambda e: self._save_langs())

        ttk.Label(langs, text='目标语言').pack(side='left')
        self.tgt_var = tk.StringVar(value=LANGS.get(self.cfg.get('tgt', 'zh'), '中文'))
        self.tgt_box = ttk.Combobox(langs, textvariable=self.tgt_var, state='readonly',
                                    values=[t for t, _ in TGT_CHOICES], width=6)
        self.tgt_box.pack(side='left', padx=6)
        self.tgt_box.bind('<<ComboboxSelected>>', lambda e: self._save_langs())

        self.pair_label = ttk.Label(root, text='', foreground='#7d828c')
        self.pair_label.grid(row=2, column=0, sticky='w', pady=(4, 0))
        self._refresh_pair()

        ttk.Label(root, text='原文（Ctrl+V 粘贴截图）').grid(row=4, column=0, sticky='w', pady=(8, 4))

        self.input = tk.Text(root, height=4, wrap='word', bg='#252830', fg='#e8eaed',
                             insertbackground='#e8eaed', relief='flat',
                             font=('Microsoft YaHei UI', 12), padx=8, pady=6)
        self.input.grid(row=5, column=0, sticky='nsew')
        self.input.bind('<Control-Return>', lambda e: self.do_translate())

        bar = ttk.Frame(root)
        bar.grid(row=6, column=0, sticky='ew', pady=8)
        ttk.Button(bar, text='翻译 (Ctrl+Enter)', command=self.do_translate).pack(side='left')
        ttk.Button(bar, text='粘贴截图', command=self.do_paste_image).pack(side='left', padx=6)
        ttk.Button(bar, text='复制译文', command=self.copy_result).pack(side='left')
        ttk.Button(bar, text='清空', command=self.clear).pack(side='right')

        ttk.Label(root, text='译文').grid(row=7, column=0, sticky='w', pady=(4, 4))
        self.output = tk.Text(root, height=5, wrap='word', bg='#252830', fg='#9fe6a0',
                              relief='flat', font=('Microsoft YaHei UI', 12), padx=8, pady=6)
        self.output.grid(row=8, column=0, sticky='nsew')

        zone = ttk.Frame(root)
        zone.grid(row=9, column=0, sticky='ew', pady=(10, 0))
        ttk.Button(zone, text='框选聊天区域', command=self.pick_region).pack(side='left')
        self.region_label = ttk.Label(zone, text=self._region_text(), foreground='#7d828c')
        self.region_label.pack(side='left', padx=8)
        ttk.Label(zone, text=f'热键 {self.cfg.get("hotkey", "F8")}', foreground='#7d828c').pack(side='right')

        ttk.Label(
            root,
            text='游戏请用「无边框窗口」；本工具不注入游戏进程，不会被反作弊波及。',
            foreground='#7d828c', wraplength=480, justify='left',
        ).grid(row=10, column=0, sticky='w', pady=(8, 0))

    # -- 语言 -------------------------------------------------------------
    def _code(self, label: str, table) -> str:
        for text, code in table:
            if text == label:
                return code
        return table[0][1]

    def _save_langs(self) -> None:
        self.cfg['src'] = self._code(self.src_var.get(), SRC_CHOICES)
        self.cfg['tgt'] = self._code(self.tgt_var.get(), TGT_CHOICES)
        if self.cfg['tgt'] != 'zh':
            self.cfg['zh_target'] = self.cfg['tgt']
        save_config(self.cfg)
        self._refresh_pair()

    def _refresh_pair(self) -> None:
        src = self._code(self.src_var.get(), SRC_CHOICES)
        tgt = self._code(self.tgt_var.get(), TGT_CHOICES)
        if src == 'auto':
            hint = f'自动识别 · 外文译中 / 中文译{LANGS.get(self.cfg.get("zh_target", "ko"), "韩语")}'
        else:
            hint = f'{LANGS.get(src, src)} → {LANGS.get(tgt, tgt)}'
        self.pair_label.configure(text=hint)

    # -- 状态 -------------------------------------------------------------
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

    # -- 预热 -------------------------------------------------------------
    def _warm_up(self) -> None:
        self.events.put(('status', '正在加载 OCR…'))
        self.ocr.warm_up()
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

    # -- 热键 -------------------------------------------------------------
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
        src_sel = self._code(self.src_var.get(), SRC_CHOICES)
        self.events.put(('status', '识别中…'))
        text = self.ocr.read(img, 'auto' if src_sel == 'auto' else src_sel)
        if not text:
            raise RuntimeError('没识别出文字，试着放大截图或换个区域')
        self.events.put(('input', text))
        self._job_translate(text, *self._decide_pair(text))

    def do_translate(self) -> None:
        text = self.input.get('1.0', 'end').strip()
        if not text:
            self.status.configure(text='先粘贴截图或输入文字', foreground='#ffb74d')
            return
        self._run_async(self._job_translate, text, *self._decide_pair(text))

    def _decide_pair(self, text: str) -> tuple[str, str]:
        src_sel = self._code(self.src_var.get(), SRC_CHOICES)
        tgt_sel = self._code(self.tgt_var.get(), TGT_CHOICES)
        src = detect_lang(text) if src_sel == 'auto' else src_sel

        if src == 'zh':
            tgt = tgt_sel if tgt_sel != 'zh' else self.cfg.get('zh_target', 'ko')
            if tgt == 'zh':
                tgt = 'ko'
        else:
            tgt = 'zh'
        return src, tgt

    def _job_translate(self, text: str, src: str, tgt: str) -> None:
        if not self.translator.ensure_server(lambda m: self.events.put(('status', m))):
            raise RuntimeError(self.translator.last_error or '模型未就绪')
        self.events.put(('status', f'翻译中（{LANGS.get(src, src)}→{LANGS.get(tgt, tgt)}）…'))
        t0 = time.time()
        out = self.translator.translate(text, src, tgt)
        self.events.put(('output', out))
        self.events.put(('done', f'完成 · {LANGS.get(src, src)}→{LANGS.get(tgt, tgt)} · {time.time() - t0:.1f}s'))

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
