# -*- coding: utf-8 -*-

import tkinter as tk
from tkinter import ttk
import threading
import queue
import time
import sys
from collections import deque
import configparser
import os

import numpy as np
import requests
import sounddevice as sd
import whisper

# --- 配置加载 ---
config = configparser.ConfigParser()

if not os.path.exists('config.ini'):
    print("错误: 配置文件 'config.ini' 不存在。\nPlease copy 'config.ini.template' to 'config.ini' and fill in your API key.")
    sys.exit(1)

config.read('config.ini')

try:
    DEEPL_API_KEY = config.get('DEEPL', 'api_key')
    SILENCE_THRESHOLD = config.getfloat('AUDIO', 'silence_threshold')
    PROCESSING_INTERVAL_SECONDS = config.getint('AUDIO', 'processing_interval_seconds')
    MODEL_TYPE = config.get('WHISPER', 'model_type')
    DEFAULT_LANGUAGE = config.get('GUI', 'default_language')
    SHOW_TRANSLATION_DEFAULT = config.getboolean('GUI', 'show_translation_by_default')
    SUBTITLE_WORD_BUFFER_SIZE = config.getint('GUI', 'subtitle_word_buffer_size', fallback=40)
except (configparser.NoSectionError, configparser.NoOptionError) as e:
    print(f"错误: 配置文件 'config.ini' 格式不正确或缺少键。 {e}")
    sys.exit(1)

# --- 翻译函数 ---
def translate_text(text, target_lang='ZH'):
    if not DEEPL_API_KEY or "YOUR_DEEPL_API_KEY" in DEEPL_API_KEY:
        return "[翻译未配置]"
    try:
        response = requests.post(
            "https://api-free.deepl.com/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            data={"text": text, "target_lang": target_lang},
            timeout=10
        )
        response.raise_for_status()
        return response.json()['translations'][0]['text']
    except requests.exceptions.RequestException as e:
        return f"[翻译错误: {e}]"
    except (KeyError, IndexError):
        return "[翻译API返回格式错误]"

# --- GUI 应用主类 ---
class CaptionApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Live Caption")
        self.root.geometry("800x200+100+100") # 增加高度以容纳滚动条和4行文本
        self.root.wm_attributes("-topmost", 1)
        self.root.overrideredirect(1)
        self.root.attributes("-alpha", 0.85)
        self.root.configure(bg='black')

        # --- 状态和配置变量 ---
        self.source_language = tk.StringVar(value=DEFAULT_LANGUAGE)
        self.show_translation = tk.BooleanVar(value=SHOW_TRANSLATION_DEFAULT)
        self.translation_text = tk.StringVar()
        self.app_running = True

        # --- 缓冲区 ---
        self.word_buffer = deque(maxlen=SUBTITLE_WORD_BUFFER_SIZE)
        self.translation_input_buffer = deque(maxlen=3)

        # --- 线程通信队列 ---
        self.results_queue = queue.Queue()

        self.setup_ui()

        # --- 启动后台处理线程 ---
        self.processing_thread = threading.Thread(target=self.audio_processing_loop, daemon=True)
        self.processing_thread.start()

        self.periodic_gui_update()
        self.enforce_topmost()

        # --- 绑定窗口拖动事件 ---
        self.root.bind("<ButtonPress-1>", self.start_move)
        self.root.bind("<ButtonRelease-1>", self.stop_move)
        self.root.bind("<B1-Motion>", self.do_move)
        self._offset_x = 0
        self._offset_y = 0

    def setup_ui(self):
        """创建所有UI组件。"""
        style = ttk.Style()
        style.theme_use('default')
        
        selected_green = "#20A020"
        style.configure('.', background='black', foreground='white')
        style.configure('TFrame', background='black')
        style.configure('TLabel', background='black', foreground='white')
        style.configure('TCheckbutton', background='black', foreground='white')
        style.map('TCheckbutton', foreground=[('selected', selected_green), ('!selected', 'white')], background=[('active', 'black')])
        style.configure('TRadiobutton', background='black', foreground='white')
        style.map('TRadiobutton', foreground=[('selected', selected_green), ('!selected', 'white')], background=[('active', 'black')])
        style.configure("Vertical.TScrollbar", background='black', troughcolor='black', bordercolor='black', arrowcolor='white')

        main_frame = ttk.Frame(self.root, style='TFrame')
        main_frame.pack(fill='both', expand=True)

        # --- 字幕显示区域 (Text + Scrollbar) ---
        subtitle_frame = ttk.Frame(main_frame, style='TFrame')
        subtitle_frame.pack(pady=(10, 0), padx=10, fill='x')

        self.subtitle_text_widget = tk.Text(subtitle_frame, height=4, font=("Helvetica", 18, "bold"), fg="white", bg="black", relief="flat", wrap="word", borderwidth=0)
        self.subtitle_text_widget.pack(side='left', fill='x', expand=True)

        scrollbar = ttk.Scrollbar(subtitle_frame, orient='vertical', command=self.subtitle_text_widget.yview, style="Vertical.TScrollbar")
        scrollbar.pack(side='right', fill='y')
        self.subtitle_text_widget['yscrollcommand'] = scrollbar.set
        
        # --- 翻译显示区域 ---
        translation_label = ttk.Label(main_frame, textvariable=self.translation_text, font=("Helvetica", 14), foreground="#00FFFF", wraplength=780, justify="left")
        translation_label.pack(pady=5, padx=10, fill='x', anchor='w')

        # --- 控制区域 ---
        control_frame = ttk.Frame(main_frame, style='TFrame')
        control_frame.pack(fill='x', padx=10, side='bottom', pady=5)

        lang_label = ttk.Label(control_frame, text="源语言:", style='TLabel')
        lang_label.pack(side='left', padx=(0, 5))
        
        eng_radio = ttk.Radiobutton(control_frame, text="英语", variable=self.source_language, value='english', style='TRadiobutton')
        eng_radio.pack(side='left')
        
        jp_radio = ttk.Radiobutton(control_frame, text="日语", variable=self.source_language, value='japanese', style='TRadiobutton')
        jp_radio.pack(side='left', padx=(0, 20))

        trans_check = ttk.Checkbutton(control_frame, text="显示翻译", variable=self.show_translation, style='TCheckbutton')
        trans_check.pack(side='left')

        quit_label = tk.Label(control_frame, text="✕", bg='black', fg='white', font=("Helvetica", 10, "bold"))
        quit_label.pack(side='right')
        quit_label.bind("<Button-1>", lambda e: self.quit_app())

    def enforce_topmost(self):
        self.root.wm_attributes("-topmost", 1)
        if self.app_running:
            self.root.after(2000, self.enforce_topmost)

    def start_move(self, event):
        self._offset_x = event.x
        self._offset_y = event.y

    def stop_move(self, event):
        self._offset_x = 0
        self._offset_y = 0

    def do_move(self, event):
        new_x = self.root.winfo_x() + (event.x - self._offset_x)
        new_y = self.root.winfo_y() + (event.y - self._offset_y)
        self.root.geometry(f"+{new_x}+{new_y}")

    def quit_app(self):
        self.app_running = False
        self.root.destroy()

    def periodic_gui_update(self):
        while not self.results_queue.empty():
            result = self.results_queue.get_nowait()
            if result['type'] == 'subtitle':
                new_words = result['text'].strip().split()
                if new_words:
                    self.word_buffer.extend(new_words)
                    display_text = " ".join(self.word_buffer)
                    
                    # 更新Text小部件内容
                    self.subtitle_text_widget.config(state='normal')
                    self.subtitle_text_widget.delete('1.0', 'end')
                    self.subtitle_text_widget.insert('end', display_text)
                    self.subtitle_text_widget.config(state='disabled')
                    self.subtitle_text_widget.see('end') # 自动滚动到底部

            elif result['type'] == 'translation':
                if self.show_translation.get():
                    self.translation_text.set(result['text'])
        
        if not self.show_translation.get():
            self.translation_text.set("")

        if self.app_running:
            self.root.after(100, self.periodic_gui_update)

    def audio_processing_loop(self):
        try:
            model = whisper.load_model(MODEL_TYPE)
            self.results_queue.put({'type': 'subtitle', 'text': '模型加载完毕，请开始讲话...'}) 
        except Exception as e:
            self.results_queue.put({'type': 'subtitle', 'text': f"模型加载失败: {e}"})
            return

        samplerate = 16000
        interval_samples = int(PROCESSING_INTERVAL_SECONDS * samplerate)
        audio_buffer = np.array([], dtype=np.float32)

        try:
            device_id = sd.default.device[0]
        except Exception as e:
            self.results_queue.put({'type': 'subtitle', 'text': f"错误: 找不到默认麦克风! {e}"})
            return

        audio_queue = queue.Queue()

        def audio_callback(indata, frames, time, status):
            if status:
                print(status, file=sys.stderr)
            if self.app_running:
                audio_queue.put(indata.copy())

        stream = sd.InputStream(device=device_id, channels=1, samplerate=samplerate, callback=audio_callback, dtype='float32')
        stream.start()

        while self.app_running:
            try:
                audio_chunk = [audio_queue.get(timeout=0.5)]
                while not audio_queue.empty():
                    audio_chunk.append(audio_queue.get_nowait())
                
                audio_buffer = np.append(audio_buffer, np.concatenate(audio_chunk).flatten())

                if len(audio_buffer) >= interval_samples:
                    processing_audio = audio_buffer[:interval_samples]
                    audio_buffer = audio_buffer[interval_samples:]

                    rms_val = np.sqrt(np.mean(np.square(processing_audio)))
                    if rms_val < SILENCE_THRESHOLD:
                        continue

                    result = model.transcribe(
                        processing_audio,
                        language=self.source_language.get(),
                        fp16=False
                    )
                    recognized_text = result['text'].strip()

                    if recognized_text:
                        self.results_queue.put({'type': 'subtitle', 'text': recognized_text})

                        if self.show_translation.get():
                            self.translation_input_buffer.append(recognized_text)
                            text_to_translate = " ".join(self.translation_input_buffer)
                            
                            def translate_task(text):
                                translated = translate_text(text)
                                if self.app_running:
                                    self.results_queue.put({'type': 'translation', 'text': translated})
                            
                            threading.Thread(target=translate_task, args=(text_to_translate,), daemon=True).start()

            except queue.Empty:
                continue
            except Exception as e:
                print(f"处理循环中发生错误: {e}", file=sys.stderr)
                time.sleep(1)

        stream.stop()
        stream.close()

# --- 程序入口 ---
if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = CaptionApp(root)
        root.mainloop()
    except Exception as e:
        print(f"应用启动失败: {e}", file=sys.stderr)
        sys.exit(1)
