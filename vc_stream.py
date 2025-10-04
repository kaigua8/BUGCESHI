# vc_stream.py
# ESP32-S3 MicroPython
# 实现与 PC test_voice_changer.py 行为一致的实时流：保持 HTTP keep-alive 会话，
# 按后端 chunk_sec 计算一次性发送帧数，流式发送 float32（由 int16 转换而来），
# 并流式接收后端返回的 float32 音频，边转 int16 边播放，避免大内存分配。

try:
    import urequests as requests
except Exception:
    requests = None

import usocket as socket
import network
import time
import struct
import gc
import math

# I2S / machine imports (adjust pins to your board)
try:
    from machine import Pin, I2S
except Exception:
    # Running off-device (for static analysis): provide dummies
    Pin = None
    I2S = None

# ---- CONFIG ----
# 关闭/打开调试日志
DEBUG = False
API_URL = "http://192.168.31.90:18000/api/voice-changer/convert_chunk_bulk"
SSID = "Xiaomi_5662"
PASSWORD = "fafafa888"

SAMPLE_RATE = 44100
CHUNK_SIZE = 1024    # 与 PC 脚本默认一致
DEFAULT_CHUNK_SEC = 0.1
BITS = 16
CHANNELS = 1

# 控制内存峰值：把要发送的大块再分成 sub_send_frames 个帧的小块逐次 send
SUB_SEND_FRAMES = 128

# I2S pins — 请按你的硬件调整
MIC_WS_PIN = 4
MIC_SCK_PIN = 5
MIC_SD_PIN  = 6
SPK_WS_PIN = 16
SPK_SCK_PIN = 15
SPK_SD_PIN  = 7
# ----------------

# 是否用本地 WAV 文件替代 I2S 采集进行测试
USE_WAV_FILE = False
WAV_PATH = "src/456.wav"

def _read_wav_info(f):
    # 简易 WAV 解析：RIFF/WAVE, 找到 'fmt ' 和 'data' chunk
    # 返回 (channels, sample_rate, bits_per_sample, data_offset, data_size)
    header = f.read(12)
    if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("Invalid WAV header")
    fmt_chunk_found = False
    data_chunk_found = False
    channels = None
    rate = None
    bits = None
    data_off = None
    data_size = None
    # 读取 chunks
    while True:
        chunk = f.read(8)
        if len(chunk) < 8:
            break
        cid = chunk[0:4]
        size = struct.unpack('<I', chunk[4:8])[0]
        if cid == b"fmt ":
            fmt = f.read(size)
            if len(fmt) < 16:
                raise ValueError("Invalid fmt chunk")
            audio_format = struct.unpack('<H', fmt[0:2])[0]
            channels = struct.unpack('<H', fmt[2:4])[0]
            rate = struct.unpack('<I', fmt[4:8])[0]
            bits = struct.unpack('<H', fmt[14:16])[0]
            fmt_chunk_found = True
        elif cid == b"data":
            data_off = f.tell()
            data_size = size
            data_chunk_found = True
            # 不读取数据，直接定位即可
            break
        else:
            # 跳过其他 chunk
            f.seek(size, 1)
    if not (fmt_chunk_found and data_chunk_found):
        raise ValueError("Missing fmt or data chunk")
    return channels, rate, bits, data_off, data_size

def connect_wifi(ssid, password, timeout_s=20):
    print(">>> Connecting WiFi:", ssid)
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if not sta.isconnected():
        sta.connect(ssid, password)
        t0 = time.ticks_ms()
        while not sta.isconnected():
            time.sleep(0.2)
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_s * 1000:
                raise OSError("WiFi connect timeout")
    print("WiFi connected:", sta.ifconfig())
    return sta

def _parse_url(url):
    proto_idx = url.find("://")
    start = proto_idx + 3 if proto_idx != -1 else 0
    p = url.find("/", start)
    hostport = url[start:p] if p != -1 else url[start:]
    path = url[p:] if p != -1 else "/"
    if ":" in hostport:
        host, port_s = hostport.split(":", 1)
        try:
            port = int(port_s)
        except:
            port = 80
    else:
        host = hostport
        port = 80
    return host, port, path

# int16_bytes -> out_bytes (float32 little-endian), in-place into out_bytes
def int16_to_float32_into(int16_bytes, out_bytes):
    n = len(int16_bytes) // 2
    mv_in = memoryview(int16_bytes)
    mv_out = memoryview(out_bytes)
    for i in range(n):
        s = int.from_bytes(mv_in[i*2:(i+1)*2], 'little')
        if s >= 32768:
            s -= 65536
        f = s / 32768.0
        struct.pack_into('<f', mv_out, i*4, f)

# float32 bytes -> int16 bytes, in-place into out_bytes
def float32_to_int16_into(f32_bytes, out_bytes):
    n = len(f32_bytes) // 4
    mv_in = memoryview(f32_bytes)
    mv_out = memoryview(out_bytes)
    for i in range(n):
        f = struct.unpack_from('<f', mv_in, i*4)[0]
        if f > 1.0:
            f = 1.0
        elif f < -1.0:
            f = -1.0
        s = int(f * 32767.0)
        if s < 0:
            s += 65536
        mv_out[i*2:(i+1)*2] = s.to_bytes(2, 'little')

# ---------- Keep-alive HTTP client (socket) ----------
class HTTPKeepAliveClient:
    def __init__(self, api_url, sub_send_frames=SUB_SEND_FRAMES):
        self.api_url = api_url
        self.host, self.port, self.path = _parse_url(api_url)
        self.sock = None
        self.sub_send_frames = sub_send_frames
        self._closed_by_server = False

    def _host_header(self):
        return self.host if self.port == 80 else f"{self.host}:{self.port}"

    def connect(self, timeout=5):
        self.close()
        try:
            s = socket.socket()
            s.settimeout(timeout)
            s.connect((self.host, self.port))
            self.sock = s
            self._closed_by_server = False
            if DEBUG:
                print("HTTP keep-alive: connected to", (self.host, self.port))
            return True
        except Exception as e:
            if DEBUG:
                print("HTTP connect failed:", e)
            self.sock = None
            return False

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _recv_line(self, maxlen=4096):
        # 读取直到 CRLF，返回不包含 CRLF 的行字节
        data = b""
        while True:
            b = self.sock.recv(1)
            if not b:
                raise OSError("socket closed while reading line")
            if b == b"\n":
                # 去掉末尾的 '\r'（如有）
                if data.endswith(b"\r"):
                    return data[:-1]
                return data
            data += b
            if len(data) > maxlen:
                raise OSError("line too long")

    def _recv_until(self, delim=b"\r\n\r\n", maxhdr=8192):
        # read until header end, return header bytes
        data = b""
        while True:
            try:
                b = self.sock.recv(1)
            except Exception as e:
                # connection closed/timeout
                raise e
            if not b:
                # remote closed
                raise OSError("socket closed while reading header")
            data += b
            if delim in data:
                return data
            if len(data) > maxhdr:
                raise OSError("header too long")

    def post_waveform_stream(self, agg_int16_mv, frames, tx_write_callback=None, timeout=10):
        """
        Send aggregated int16 memoryview (little-endian 2 bytes per frame) as float32 payload via multipart/form-data.
        Stream the response and call tx_write_callback(pcm_bytes) with int16 pcm bytes chunks as they arrive.
        Keeps socket open (Connection: keep-alive) unless server specifies close.
        Returns (status_code, response_bytes_count) or (None, 0) on error.
        """
        if not self.sock:
            if not self.connect():
                return None, 0

        try:
            boundary = "----vcboundary12345"
            head = (
                "--" + boundary + "\r\n"
                'Content-Disposition: form-data; name="waveform"; filename="chunk.bin"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n'
            ).encode('utf-8')
            tail = ("\r\n--" + boundary + "--\r\n").encode('utf-8')
            payload_len = frames * 4
            content_length = len(head) + payload_len + len(tail)

            host_header = self._host_header()
            header = (
                "POST " + self.path + " HTTP/1.1\r\n"
                "Host: " + host_header + "\r\n"
                "Content-Type: multipart/form-data; boundary=" + boundary + "\r\n"
                "Content-Length: " + str(content_length) + "\r\n"
                "Connection: keep-alive\r\n"
                "x-timestamp: " + str(int(time.time() * 1000)) + "\r\n"
                "\r\n"
            ).encode('utf-8')

            # send header+head
            self.sock.send(header)
            self.sock.send(head)

            # stream-convert int16 -> float32 and send in small blocks
            mv = memoryview(agg_int16_mv)
            sub = self.sub_send_frames
            for i in range(0, frames, sub):
                n_sub = min(sub, frames - i)
                sb = bytearray(n_sub * 4)
                off = i * 2
                for j in range(n_sub):
                    s = int.from_bytes(mv[off + j*2: off + (j+1)*2], 'little')
                    if s >= 32768:
                        s -= 65536
                    f = s / 32768.0
                    struct.pack_into('<f', sb, j*4, f)
                # send the small float32 block
                self.sock.send(sb)
                # small GC occasionally
                if (i // sub) & 0x7 == 0:
                    gc.collect()

            # tail
            self.sock.send(tail)

            # read response header
            hdr = self._recv_until()
            try:
                hdr_s = hdr.decode('utf-8', 'ignore')
            except:
                hdr_s = str(hdr)
            if DEBUG:
                # print response header (helpful debugging)
                print("=== HTTP Resp Header ===")
                print(hdr_s)

            # parse status and content-length and connection header
            status_code = 0
            content_len = None
            transfer_chunked = False
            connection_header = ""
            for ln in hdr_s.split("\r\n"):
                if ln.startswith("HTTP/"):
                    parts = ln.split(" ")
                    if len(parts) >= 2:
                        try:
                            status_code = int(parts[1])
                        except:
                            status_code = 0
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    if k.lower().strip() == "content-length":
                        try:
                            content_len = int(v.strip())
                        except:
                            content_len = None
                    if k.lower().strip() == "transfer-encoding" and "chunked" in v.lower():
                        transfer_chunked = True
                    if k.lower().strip() == "connection":
                        connection_header = v.strip().lower()

            # Read response body, stream-process float32 -> int16 and call tx callback with pcm chunks
            bytes_read = 0
            if content_len is not None and not transfer_chunked:
                to_read = content_len
                buf = b""
                # read fixed amount
                while to_read > 0:
                    chunk = self.sock.recv(1024)
                    if not chunk:
                        # server closed unexpectedly
                        self._closed_by_server = True
                        break
                    bytes_read += len(chunk)
                    to_read -= len(chunk)
                    buf += chunk
                    # process whole 4-bytes aligned chunks only
                    proc_len = (len(buf) // 4) * 4
                    if proc_len > 0:
                        pos = 0
                        # process in smaller blocks to control memory
                        max_proc = 512 * 4
                        while pos < proc_len:
                            sub_proc_len = min(max_proc, proc_len - pos)
                            fsub = buf[pos: pos + sub_proc_len]
                            pcm_len = len(fsub) // 2
                            pcm = bytearray(pcm_len)
                            float32_to_int16_into(fsub, pcm)
                            if tx_write_callback:
                                try:
                                    tx_write_callback(pcm)
                                except Exception:
                                    pass
                            pos += sub_proc_len
                        if (bytes_read & 0x1FFF) == 0:
                            gc.collect()
                        # keep remainder
                        buf = buf[proc_len:]
                # finished reading
            elif transfer_chunked:
                # RFC 7230 chunked transfer encoding
                buf = b""
                while True:
                    # read chunk-size line
                    line = self._recv_line()
                    if not line:
                        # empty line between chunks may appear; read again
                        continue
                    try:
                        size = int(line.decode('utf-8').split(";", 1)[0], 16)
                    except Exception:
                        size = 0
                    if size == 0:
                        # read trailing CRLF (possibly trailer headers)
                        # absorb until empty line
                        # many servers send just one blank line
                        try:
                            _ = self._recv_line()
                        except Exception:
                            pass
                        break
                    remaining = size
                    while remaining > 0:
                        chunk = self.sock.recv(min(1024, remaining))
                        if not chunk:
                            self._closed_by_server = True
                            remaining = 0
                            break
                        bytes_read += len(chunk)
                        remaining -= len(chunk)
                        buf += chunk
                        proc_len = (len(buf) // 4) * 4
                        if proc_len > 0:
                            pos = 0
                            max_proc = 512 * 4
                            while pos < proc_len:
                                sub_proc_len = min(max_proc, proc_len - pos)
                                fsub = buf[pos: pos + sub_proc_len]
                                pcm_len = len(fsub) // 2
                                pcm = bytearray(pcm_len)
                                float32_to_int16_into(fsub, pcm)
                                if tx_write_callback:
                                    try:
                                        tx_write_callback(pcm)
                                    except Exception:
                                        pass
                                pos += sub_proc_len
                            buf = buf[proc_len:]
                        if (bytes_read & 0x1FFF) == 0:
                            gc.collect()
                    # each chunk is terminated by CRLF; consume it
                    try:
                        _ = self.sock.recv(2)
                    except Exception:
                        pass
            else:
                # no content-length, read until close
                buf = b""
                while True:
                    chunk = self.sock.recv(1024)
                    if not chunk:
                        self._closed_by_server = True
                        break
                    bytes_read += len(chunk)
                    buf += chunk
                    proc_len = (len(buf) // 4) * 4
                    if proc_len > 0:
                        pos = 0
                        max_proc = 512 * 4
                        while pos < proc_len:
                            sub_proc_len = min(max_proc, proc_len - pos)
                            fsub = buf[pos: pos + sub_proc_len]
                            pcm_len = len(fsub) // 2
                            pcm = bytearray(pcm_len)
                            float32_to_int16_into(fsub, pcm)
                            if tx_write_callback:
                                try:
                                    tx_write_callback(pcm)
                                except Exception:
                                    pass
                            pos += sub_proc_len
                        buf = buf[proc_len:]
                    if (bytes_read & 0x1FFF) == 0:
                        gc.collect()

            # if server indicated Connection: close, then we must close our socket
            if connection_header == "close" or self._closed_by_server:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

            gc.collect()
            return status_code, bytes_read
        except Exception as e:
            if DEBUG:
                print("HTTP socket error while posting:", e)
            # close socket to force reconnect next time
            try:
                if self.sock:
                    self.sock.close()
            except:
                pass
            self.sock = None
            gc.collect()
            return None, 0

# ---------- I2S helpers ----------
def create_tx(rate, bits, channels):
    if I2S is None:
        return None
    fmt = I2S.MONO if channels == 1 else I2S.STEREO
    return I2S(
        0,
        sck=Pin(SPK_SCK_PIN),
        ws=Pin(SPK_WS_PIN),
        sd=Pin(SPK_SD_PIN),
        mode=I2S.TX,
        bits=bits,
        format=fmt,
        rate=rate,
        ibuf=8 * 1024
    )

def create_rx(rate, bits, channels):
    if I2S is None:
        return None
    fmt = I2S.MONO if channels == 1 else I2S.STEREO
    return I2S(
        1,
        sck=Pin(MIC_SCK_PIN),
        ws=Pin(MIC_WS_PIN),
        sd=Pin(MIC_SD_PIN),
        mode=I2S.RX,
        bits=bits,
        format=fmt,
        rate=rate,
        ibuf=8 * 1024
    )

# ---------- Server info helper (uses urequests if available) ----------
def get_server_chunk_sec(default_sec=DEFAULT_CHUNK_SEC):
    info_url = API_URL.replace("/api/voice-changer/convert_chunk_bulk",
                               "/api/voice-changer-manager/information")
    if requests is None:
        return default_sec
    try:
        r = requests.get(info_url, timeout=2)
        if r and r.status_code == 200:
            obj = None
            try:
                obj = r.json()
            except Exception:
                obj = None
            r.close()
            sec = None
            if obj:
                sec = obj.get("chunk_sec", None)
                if sec is None:
                    try:
                        vcpl = obj.get("vc_pipeline", None)
                        if vcpl:
                            sec = vcpl.get("chunk_sec", None)
                    except Exception:
                        sec = None
            if isinstance(sec, (int, float)) and sec > 0:
                return float(sec)
        return default_sec
    except Exception as e:
        # failed to get server info
        # print("get_server_chunk_sec error:", e)
        return default_sec

# ---------- Helpers ----------
def _i2s_read_exact(rx, target_buf):
    # 尝试把 target_buf 填满，避免短读造成聚合错位
    if rx is None:
        return 0
    mv = memoryview(target_buf)
    total = 0
    want = len(target_buf)
    while total < want:
        try:
            n = rx.readinto(mv[total:])
        except Exception:
            n = 0
        if not n:
            time.sleep(0.001)
            continue
        total += n
    return total

def _i2s_write_silence(tx, frames, channels=1, chunk_frames=CHUNK_SIZE):
    # 播放静音，维持节拍
    if tx is None:
        return
    silence = b"\x00\x00" * (chunk_frames * channels)
    remaining = frames
    while remaining > 0:
        step = chunk_frames if remaining >= chunk_frames else remaining
        try:
            tx.write(silence[:step * 2 * channels])
        except Exception:
            pass
        remaining -= step
        if (remaining & 0x3FF) == 0:
            gc.collect()

# ---------- Main real-time flow ----------
def main():
    # WiFi
    try:
        connect_wifi(SSID, PASSWORD)
    except Exception as e:
        print("WiFi connect failed:", e)
        return

    # create I2S
    tx = create_tx(SAMPLE_RATE, BITS, CHANNELS)
    rx = None if USE_WAV_FILE else create_rx(SAMPLE_RATE, BITS, CHANNELS)

    # compute aggregation to match server chunk_sec like test_voice_changer.py
    chunk_sec = get_server_chunk_sec(DEFAULT_CHUNK_SEC)
    required_frames = int(math.ceil(SAMPLE_RATE * chunk_sec))
    agg_chunks_cur = max(1, int(math.ceil(required_frames / CHUNK_SIZE)))
    if DEBUG:
        print("Server chunk_sec:", chunk_sec, "agg_chunks:", agg_chunks_cur)

    # allocate aggregation buffer (int16)
    agg_frames = agg_chunks_cur * CHUNK_SIZE
    try:
        agg_int16 = bytearray(agg_frames * 2 * CHANNELS)
    except Exception as e:
        print("Alloc agg buffer failed:", e)
        return

    client = HTTPKeepAliveClient(API_URL, sub_send_frames=SUB_SEND_FRAMES)

    # Initial connection test (mimic PC script behavior)
    try:
        # test payload = zeros of CHUNK_SIZE frames (float32) as PC does
        # We'll use keep-alive client but send a small test to establish the connection
        test_frames = CHUNK_SIZE
        test_int16_mv = memoryview(b'\x00\x00' * test_frames)
        # Use a transient callback that collects bytes count only (no playback)
        def _nop_write(b): pass
        status, nbytes = client.post_waveform_stream(test_int16_mv, test_frames, tx_write_callback=_nop_write)
    if DEBUG:
        print("Connection test -> status:", status, "resp bytes:", nbytes)
    except Exception as e:
        print("Connection test failed:", e)

    # main loop: I2S mic or WAV file, aggregate and send
    try:
        if USE_WAV_FILE:
            with open(WAV_PATH, "rb") as f:
                ch, rate, bits, data_off, data_size = _read_wav_info(f)
                if DEBUG:
                    print("WAV info:", ch, rate, bits, data_off, data_size)
                if bits != 16 and DEBUG:
                    print("Warning: WAV bits is not 16, current code expects 16-bit PCM")
                if rate != SAMPLE_RATE and DEBUG:
                    print("Warning: WAV sample rate", rate, "!=", SAMPLE_RATE)
                if ch != CHANNELS and DEBUG:
                    print("Warning: WAV channels", ch, "!=", CHANNELS)
                f.seek(data_off)
                mic_buf = bytearray(CHUNK_SIZE * (BITS // 8) * CHANNELS)
                loop_count = 0
                total_read = 0
                while True:
                    n = f.readinto(mic_buf)
                    if n is None or n <= 0:
                        break
                    loop_count += 1
                    total_read += n
                    idx = ((loop_count - 1) % agg_chunks_cur) * len(mic_buf)
                    agg_int16[idx: idx + len(mic_buf)] = mic_buf

                    if (loop_count % agg_chunks_cur) == 0:
                        cur_frames = agg_frames
                        cur_mv = memoryview(agg_int16)[:cur_frames * 2 * CHANNELS]
                        if DEBUG:
                            print("Sending aggregated frames:", cur_frames, "bytes payload approx:", cur_frames * 4)
                        def tx_write(pcm_bytes):
                            if tx:
                                try:
                                    tx.write(pcm_bytes)
                                except Exception:
                                    pass
                        status, nbytes = client.post_waveform_stream(cur_mv, cur_frames, tx_write_callback=tx_write)
                        if (status is None) or (status != 200) or (nbytes == 0):
                            _i2s_write_silence(tx, cur_frames, CHANNELS, CHUNK_SIZE)
                        if DEBUG:
                            print("post status:", status, "resp bytes:", nbytes)
                        gc.collect()
                if DEBUG:
                    print("WAV streaming done, bytes read:", total_read)
        else:
            # I2S capture path
            mic_buf = bytearray(CHUNK_SIZE * (BITS // 8) * CHANNELS)
            loop_count = 0
            while True:
                n = _i2s_read_exact(rx, mic_buf)
                if n <= 0:
                    continue
                loop_count += 1
                idx = ((loop_count - 1) % agg_chunks_cur) * len(mic_buf)
                agg_int16[idx: idx + len(mic_buf)] = mic_buf

                if (loop_count % agg_chunks_cur) == 0:
                    cur_frames = agg_frames
                    cur_mv = memoryview(agg_int16)[:cur_frames * 2 * CHANNELS]
                    def tx_write(pcm_bytes):
                        if tx:
                            try:
                                tx.write(pcm_bytes)
                            except Exception:
                                pass
                    status, nbytes = client.post_waveform_stream(cur_mv, cur_frames, tx_write_callback=tx_write)
                    if (status is None) or (status != 200) or (nbytes == 0):
                        _i2s_write_silence(tx, cur_frames, CHANNELS, CHUNK_SIZE)
                    if DEBUG:
                        print("post status:", status, "resp bytes:", nbytes)
                    gc.collect()
    except KeyboardInterrupt:
        if DEBUG:
            print("Exit by user")
    except Exception as e:
        print("Runtime error:", e)
    finally:
        client.close()
        try:
            if tx:
                tx.deinit()
        except Exception:
            pass
        try:
            if rx:
                rx.deinit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
