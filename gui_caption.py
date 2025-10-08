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

# --- 辅助函数：列出音频设备 ---
def list_audio_devices():
    """在控制台打印所有可用的音频设备及其ID。"""
    print("\n" + "-"*30)
    print("可用的音频设备列表:")
    try:
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                print(f"  ID {i}: {device['name']}")
    except Exception as e:
        print(f"无法查询音频设备: {e}")
    print("-"*30 + "\n")

# --- 配置加载 ---
config = configparser.ConfigParser()

if not os.path.exists('config.ini'):
    print("错误: 配置文件 'config.ini' 不存在。\nPlease copy 'config.ini.template' to 'config.ini' and fill in your API key.")
    sys.exit(1)

config.read('config.ini')

try:
    DEEPL_API_KEY = config.get('DEEPL', 'api_key')
    mic_device_id_str = config.get('AUDIO', 'mic_device_id', fallback='').strip()
    MIC_DEVICE_ID = int(mic_device_id_str) if mic_device_id_str else None
    system_audio_device_id_str = config.get('AUDIO', 'system_audio_device_id', fallback='').strip()
    SYSTEM_AUDIO_DEVICE_ID = int(system_audio_device_id_str) if system_audio_device_id_str else None
    SILENCE_THRESHOLD = config.getfloat('AUDIO', 'silence_threshold')
    PROCESSING_INTERVAL_SECONDS = config.getint('AUDIO', 'processing_interval_seconds')
    MODEL_TYPE = config.get('WHISPER', 'model_type')
    DEFAULT_LANGUAGE = config.get('GUI', 'default_language')
    SHOW_TRANSLATION_DEFAULT = config.getboolean('GUI', 'show_translation_by_default')
    SUBTITLE_WORD_BUFFER_SIZE = config.getint('GUI', 'subtitle_word_buffer_size', fallback=40)
except Exception as e:
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
        self.root.geometry("800x200+100+100")
        self.root.wm_attributes("-topmost", 1)
        self.root.overrideredirect(1)
        self.root.attributes("-alpha", 0.85)
        self.root.configure(bg='black')

        # --- 状态和配置变量 ---
        self.source_language = tk.StringVar(value=DEFAULT_LANGUAGE)
        self.show_translation = tk.BooleanVar(value=SHOW_TRANSLATION_DEFAULT)
        self.audio_source_mode = tk.StringVar(value="系统音频")
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

        # --- 字幕显示区域 ---
        subtitle_frame = ttk.Frame(main_frame, style='TFrame')
        subtitle_frame.pack(pady=(5,0), padx=10, fill='x')
        self.subtitle_text_widget = tk.Text(subtitle_frame, height=4, font=("Helvetica", 16, "bold"), fg="white", bg="black", relief="flat", wrap="word", borderwidth=0)
        self.subtitle_text_widget.pack(side='left', fill='x', expand=True)
        scrollbar = ttk.Scrollbar(subtitle_frame, orient='vertical', command=self.subtitle_text_widget.yview, style="Vertical.TScrollbar")
        scrollbar.pack(side='right', fill='y')
        self.subtitle_text_widget['yscrollcommand'] = scrollbar.set

        # --- 翻译显示区域 ---
        translation_label = ttk.Label(main_frame, textvariable=self.translation_text, font=("Helvetica", 14), foreground="#00FFFF", wraplength=780, justify="left")
        translation_label.pack(pady=(0,5), padx=10, fill='x', anchor='w')

        # --- 控制区域 ---
        control_frame = ttk.Frame(main_frame, style='TFrame')
        control_frame.pack(fill='x', padx=10, side='bottom', pady=5)

        source_label = ttk.Label(control_frame, text="音频源:", style='TLabel')
        source_label.pack(side='left', padx=(0,5))
        mic_radio = ttk.Radiobutton(control_frame, text="麦克风", variable=self.audio_source_mode, value="麦克风", style='TRadiobutton')
        mic_radio.pack(side='left')
        sys_radio = ttk.Radiobutton(control_frame, text="系统音频", variable=self.audio_source_mode, value="系统音频", style='TRadiobutton')
        sys_radio.pack(side='left')
        mix_radio = ttk.Radiobutton(control_frame, text="混合模式", variable=self.audio_source_mode, value="混合模式", style='TRadiobutton')
        mix_radio.pack(side='left', padx=(0, 15))

        lang_label = ttk.Label(control_frame, text="源语言:", style='TLabel')
        lang_label.pack(side='left', padx=(0, 5))
        eng_radio = ttk.Radiobutton(control_frame, text="英语", variable=self.source_language, value='english', style='TRadiobutton')
        eng_radio.pack(side='left')
        jp_radio = ttk.Radiobutton(control_frame, text="日语", variable=self.source_language, value='japanese', style='TRadiobutton')
        jp_radio.pack(side='left', padx=(0, 15))

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
                    self.subtitle_text_widget.config(state='normal')
                    self.subtitle_text_widget.delete('1.0', 'end')
                    self.subtitle_text_widget.insert('end', display_text)
                    self.subtitle_text_widget.config(state='disabled')
                    self.subtitle_text_widget.see('end')
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
            self.results_queue.put({'type': 'subtitle', 'text': '模型加载完毕，请选择音频源...'}) 
        except Exception as e:
            self.results_queue.put({'type': 'subtitle', 'text': f"模型加载失败: {e}"})
            return

        mic_audio_queue = queue.Queue()
        system_audio_queue = queue.Queue()
        last_mode = ""
        streams = []
        audio_buffer = np.array([], dtype=np.float32)
        samplerate = 16000
        interval_samples = int(PROCESSING_INTERVAL_SECONDS * samplerate)

        while self.app_running:
            try:
                current_mode = self.audio_source_mode.get()
                if current_mode != last_mode:
                    for stream in streams:
                        stream.stop()
                        stream.close()
                    streams = []
                    audio_buffer = np.array([], dtype=np.float32)
                    last_mode = current_mode
                    self.results_queue.put({'type': 'subtitle', 'text': f'切换到 {current_mode} 模式...'}) 
                    self.word_buffer.clear()
                    self.translation_input_buffer.clear()
                    
                    def mic_callback(indata, frames, time, status): mic_audio_queue.put(indata.copy())
                    def system_callback(indata, frames, time, status): system_audio_queue.put(indata.copy())

                    if current_mode in ["麦克风", "混合模式"] and MIC_DEVICE_ID is not None:
                        streams.append(sd.InputStream(device=MIC_DEVICE_ID, channels=1, samplerate=samplerate, callback=mic_callback, dtype='float32'))
                    if current_mode in ["系统音频", "混合模式"] and SYSTEM_AUDIO_DEVICE_ID is not None:
                        streams.append(sd.InputStream(device=SYSTEM_AUDIO_DEVICE_ID, channels=1, samplerate=samplerate, callback=system_callback, dtype='float32'))

                    if not streams:
                        self.results_queue.put({'type': 'subtitle', 'text': f"'{current_mode}' 模式无法启动，请在 config.ini 中配置设备ID。"}) 
                        last_mode = "" # 允许重试
                        time.sleep(2)
                        continue

                    for stream in streams:
                        stream.start()

                # --- 音频数据收集与缓冲 ---
                mic_chunks = []
                while not mic_audio_queue.empty(): mic_chunks.append(mic_audio_queue.get())
                
                system_chunks = []
                while not system_audio_queue.empty(): system_chunks.append(system_audio_queue.get())

                if mic_chunks or system_chunks:
                    mixed_audio_chunk = np.array([], dtype=np.float32)
                    if mic_chunks:
                        mixed_audio_chunk = np.concatenate(mic_chunks).flatten()
                    if system_chunks:
                        system_audio_chunk = np.concatenate(system_chunks).flatten()
                        if len(mixed_audio_chunk) == 0:
                            mixed_audio_chunk = system_audio_chunk
                        else:
                            if len(mixed_audio_chunk) < len(system_audio_chunk):
                                mixed_audio_chunk = np.pad(mixed_audio_chunk, (0, len(system_audio_chunk) - len(mixed_audio_chunk)))
                            if len(system_audio_chunk) < len(mixed_audio_chunk):
                                system_audio_chunk = np.pad(system_audio_chunk, (0, len(mixed_audio_chunk) - len(system_audio_chunk)))
                            mixed_audio_chunk += system_audio_chunk
                    
                    audio_buffer = np.append(audio_buffer, mixed_audio_chunk)

                # --- 按时间间隔处理音频 ---
                if len(audio_buffer) >= interval_samples:
                    processing_audio = audio_buffer[:interval_samples]
                    audio_buffer = audio_buffer[interval_samples:]

                    rms_val = np.sqrt(np.mean(np.square(processing_audio)))
                    if rms_val < SILENCE_THRESHOLD:
                        continue

                    result = model.transcribe(processing_audio, language=self.source_language.get(), fp16=False)
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
                else:
                    time.sleep(0.1) # 如果没有足够数据，短暂休眠

            except Exception as e:
                print(f"音频处理循环错误: {e}", file=sys.stderr)
                self.results_queue.put({'type': 'subtitle', 'text': f'发生错误: {e}'})
                time.sleep(2)

        for stream in streams:
            stream.stop()
            stream.close()

# --- 程序入口 ---
if __name__ == "__main__":
    if MIC_DEVICE_ID is None or SYSTEM_AUDIO_DEVICE_ID is None:
        list_audio_devices()
        print("提示: 请将找到的设备ID填入 config.ini 文件中。")
        if input("是否继续启动GUI？(y/n): ").lower() != 'y':
            sys.exit(0)

    try:
        root = tk.Tk()
        app = CaptionApp(root)
        root.mainloop()
    except Exception as e:
        print(f"应用启动失败: {e}", file=sys.stderr)
        sys.exit(1)