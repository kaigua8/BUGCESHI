import threading
import queue
import time
import sys

import requests
import numpy as np
try:
    import pyaudio
except ImportError:
    pyaudio = None

"""
RVC Voice Changer 实时客户端
- 选择 MME 输入/输出设备
- 采集麦克风音频，调用 convert_chunk_bulk 接口
- 将返回的音频播放到所选输出设备
"""

API_URL = "http://192.168.31.90:18000/api/voice-changer/convert_chunk_bulk"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_SIZE = 1024  # 每次采样的帧数


def _ensure_pyaudio():
    if pyaudio is None:
        print("未找到 pyaudio，请先安装依赖：pip install -r requirements_test.txt 或 pip install pyaudio numpy requests")
        sys.exit(1)


def list_mme_devices(pa: "pyaudio.PyAudio"):
    hostapis = {}
    for i in range(pa.get_host_api_count()):
        info = pa.get_host_api_info_by_index(i)
        hostapis[i] = info.get("name", "")
    mme_inputs = []
    mme_outputs = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        hostapi_name = hostapis.get(info.get("hostApi", -1), "")
        if hostapi_name.upper() == "MME":
            # 输入设备
            if int(info.get("maxInputChannels", 0)) > 0:
                mme_inputs.append({
                    "index": i,
                    "name": info.get("name", f"Device {i}"),
                    "channels": int(info.get("maxInputChannels", 0)),
                    "defaultSampleRate": int(info.get("defaultSampleRate", DEFAULT_SAMPLE_RATE))
                })
            # 输出设备
            if int(info.get("maxOutputChannels", 0)) > 0:
                mme_outputs.append({
                    "index": i,
                    "name": info.get("name", f"Device {i}"),
                    "channels": int(info.get("maxOutputChannels", 0)),
                    "defaultSampleRate": int(info.get("defaultSampleRate", DEFAULT_SAMPLE_RATE))
                })
    return mme_inputs, mme_outputs


def pick_device(devices, title: str):
    if not devices:
        print(f"未找到MME {title}设备，请确认系统已启用MME设备。")
        sys.exit(1)
    print(f"可用的MME {title}设备：")
    for idx, dev in enumerate(devices):
        print(f"  [{idx}] {dev['name']} (index={dev['index']}, channels={dev['channels']}, defaultSR={dev['defaultSampleRate']})")
    while True:
        try:
            sel = int(input(f"请选择{title}设备编号: ").strip())
            if 0 <= sel < len(devices):
                return devices[sel]
        except Exception:
            pass
        print("输入不合法，请重新输入有效编号。")


class RealTimeClient:
    def __init__(self, api_url: str, sample_rate: int = DEFAULT_SAMPLE_RATE, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.api_url = api_url
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "*/*",
            "Connection": "keep-alive",
        })
        self.pa = pyaudio.PyAudio()
        self.in_stream = None
        self.out_stream = None
        self.record_queue: queue.Queue[bytes] = queue.Queue(maxsize=20)
        self.play_queue: queue.Queue[bytes] = queue.Queue(maxsize=20)
        self._stop_flag = threading.Event()
        self._threads: list[threading.Thread] = []

    def setup_streams(self, input_dev_index: int, output_dev_index: int):
        # 输入流：float32 单声道
        self.in_stream = self.pa.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self.sample_rate,
            input=True,
            frames_per_buffer=self.chunk_size,
            input_device_index=input_dev_index,
        )
        # 输出流：float32 单声道
        self.out_stream = self.pa.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=self.chunk_size,
            output_device_index=output_dev_index,
        )

    def _record_loop(self):
        while not self._stop_flag.is_set():
            try:
                data = self.in_stream.read(self.chunk_size, exception_on_overflow=False)
                # data 为 float32 的原始字节
                try:
                    self.record_queue.put(data, timeout=0.1)
                except queue.Full:
                    # 丢弃最旧数据避免积压
                    _ = self.record_queue.get_nowait()
                    self.record_queue.put_nowait(data)
            except Exception:
                time.sleep(0.005)

    def _process_loop(self):
        while not self._stop_flag.is_set():
            try:
                chunk = self.record_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                # 以 multipart/form-data 发送 float32 字节
                files = {
                    "waveform": ("chunk.bin", chunk, "application/octet-stream"),
                }
                headers = {
                    "x-timestamp": str(int(time.time() * 1000))
                }
                resp = self.session.post(self.api_url, files=files, headers=headers, timeout=5)
                if resp.status_code == 200 and resp.content:
                    out_bytes = resp.content
                    # 服务端返回的是 float32 字节，直接播放；如需安全处理可做长度对齐
                    if len(out_bytes) % 4 != 0:
                        # 对齐到 4 的倍数，避免播放异常
                        out_bytes = out_bytes[:len(out_bytes) - (len(out_bytes) % 4)]
                    try:
                        self.play_queue.put(out_bytes, timeout=0.1)
                    except queue.Full:
                        _ = self.play_queue.get_nowait()
                        self.play_queue.put_nowait(out_bytes)
                else:
                    # 返回异常时，输出静音数据避免卡顿
                    silent = np.zeros(self.chunk_size, dtype=np.float32).tobytes()
                    try:
                        self.play_queue.put(silent, timeout=0.1)
                    except queue.Full:
                        _ = self.play_queue.get_nowait()
                        self.play_queue.put_nowait(silent)
            except Exception:
                # 网络异常时，输出静音以维持播放连续性
                silent = np.zeros(self.chunk_size, dtype=np.float32).tobytes()
                try:
                    self.play_queue.put(silent, timeout=0.1)
                except queue.Full:
                    _ = self.play_queue.get_nowait()
                    self.play_queue.put_nowait(silent)

    def _play_loop(self):
        while not self._stop_flag.is_set():
            try:
                out = self.play_queue.get(timeout=0.1)
            except queue.Empty:
                # 没有数据时播放静音保持时钟
                out = np.zeros(self.chunk_size, dtype=np.float32).tobytes()
            try:
                self.out_stream.write(out)
            except Exception:
                time.sleep(0.001)

    def start(self):
        self._stop_flag.clear()
        t_rec = threading.Thread(target=self._record_loop, name="rec", daemon=True)
        t_proc = threading.Thread(target=self._process_loop, name="proc", daemon=True)
        t_play = threading.Thread(target=self._play_loop, name="play", daemon=True)
        self._threads = [t_rec, t_proc, t_play]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop_flag.set()
        for t in self._threads:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        try:
            if self.in_stream:
                self.in_stream.stop_stream()
                self.in_stream.close()
        except Exception:
            pass
        try:
            if self.out_stream:
                self.out_stream.stop_stream()
                self.out_stream.close()
        except Exception:
            pass
        try:
            self.pa.terminate()
        except Exception:
            pass
        try:
            self.session.close()
        except Exception:
            pass


def main():
    _ensure_pyaudio()

    print("RVC Voice Changer 实时客户端")
    print(f"接口: {API_URL}")

    pa = pyaudio.PyAudio()
    mme_inputs, mme_outputs = list_mme_devices(pa)
    pa.terminate()

    in_dev = pick_device(mme_inputs, "输入")
    out_dev = pick_device(mme_outputs, "输出")

    sr = DEFAULT_SAMPLE_RATE
    try:
        user_sr = input(f"请输入采样率(回车使用默认 {DEFAULT_SAMPLE_RATE}): ").strip()
        if user_sr:
            sr = int(user_sr)
    except Exception:
        sr = DEFAULT_SAMPLE_RATE

    chunk = DEFAULT_CHUNK_SIZE
    try:
        user_chunk = input(f"请输入块大小(回车使用默认 {DEFAULT_CHUNK_SIZE}): ").strip()
        if user_chunk:
            chunk = int(user_chunk)
    except Exception:
        chunk = DEFAULT_CHUNK_SIZE

    client = RealTimeClient(API_URL, sample_rate=sr, chunk_size=chunk)
    client.setup_streams(in_dev["index"], out_dev["index"])

    # 连接测试
    print("正在测试与后端的连接...")
    try:
        test_bytes = np.zeros(chunk, dtype=np.float32).tobytes()
        resp = client.session.post(API_URL, files={"waveform": ("test.bin", test_bytes, "application/octet-stream")}, headers={"x-timestamp": str(int(time.time() * 1000))}, timeout=5)
        if resp.status_code == 200:
            print("连接正常，开始实时处理。按 Ctrl+C 结束。")
        else:
            print(f"连接测试失败，状态码: {resp.status_code}，仍将尝试开始实时处理。")
    except Exception as e:
        print(f"连接测试异常: {e}，仍将尝试开始实时处理。")

    client.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("正在停止...")
    finally:
        client.stop()
        print("已停止。")


if __name__ == "__main__":
    main()