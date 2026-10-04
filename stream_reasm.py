#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stream_reasm.py — 多逻辑连接字节流报文还原工具（纯 Python 标准库，单文件）

帧格式（全部大端）：
    +-------+--------+-----+--------+===========+----------+
    | magic | stream | seq | length | payload   | checksum |
    | 2B    | 1B     | 2B  | 2B     | length 字节| 1B       |
    +-------+--------+-----+--------+===========+----------+
    magic    = 0xA5 0x5A          帧同步字（用于错位后重新同步）
    stream   = 流标识 0..255      帧按它归组，跨流交错互不污染
    seq      = 流内帧序号          用于缺口 / 重复检测
    length   = payload 字节数      切帧依据（解决拆半 / 粘包）
    checksum = (stream..payload 所有字节之和) & 0xFF

功能：
  * 帧拆半：解析器缓存不完整字节，待后续分片到达再切帧
  * 粘包：一个分片内按 length 逐帧切出
  * 序号缺口：报告流标识与缺失序号
  * 重复序号：报告流标识与重复序号
  * 流结束仍有半帧：报告不完整帧（收尾规则：输入结束即视为流结束）
  * 校验错误：定位到流与帧序号，丢弃该帧载荷
  * 输出：按流归组的还原报文 + 错误清单

用法：
  python3 stream_reasm.py --demo                 # 内置示例（覆盖全部错误场景）
  python3 stream_reasm.py input.json [--json]    # 从 JSON 输入运行

输入 JSON（二选一）：
  {"hex": "a55a...", "chunk_sizes": [5, 9, 3]}        # 整流 + 自定义分片
  {"chunks_hex": ["a55a...", "0100..."]}              # 直接给出各分片
  {"messages": [{"stream": 1, "seq": 0, "text": "hi"}]}  # 由逻辑报文生成帧流

--demo 输出示例（节选）：
  === 流 1 ===
    报文(text): Hello, world!
    完整: 否（序号 0..3 中存在缺口）
  === 错误清单 ===
  [#1] offset=0x1A stream=2 seq=1 checksum_error: 期望 0x3F 实际 0xC0，载荷已丢弃
  [#2] offset=0x27 stream=1 seq=1 duplicate_seq: 重复序号，已忽略
  ...
"""

import argparse
import json
import sys

MAGIC = b"\xA5\x5A"
HEADER_LEN = 7          # magic(2) + stream(1) + seq(2) + length(2)
MAX_GAP_REPORT = 1000   # 单个缺口最多逐条列出的序号数


# ---------------------------------------------------------------- 帧编解码

def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def encode_frame(stream_id: int, seq: int, payload: bytes) -> bytes:
    if not 0 <= stream_id <= 0xFF:
        raise ValueError("stream_id 需在 0..255")
    if not 0 <= seq <= 0xFFFF:
        raise ValueError("seq 需在 0..65535")
    if len(payload) > 0xFFFF:
        raise ValueError("payload 过长")
    head = MAGIC + bytes([stream_id]) + seq.to_bytes(2, "big") \
        + len(payload).to_bytes(2, "big")
    return head + payload + bytes([checksum(head[2:] + payload)])


# ---------------------------------------------------------------- 增量切帧器

class FrameParser:
    """按 length 字段从任意分片的字节流中切出完整帧。

    拆半：内部缓冲区保留未凑齐的字节；粘包：循环切出直到剩余不足一帧。
    """

    def __init__(self, on_frame, on_error):
        self._buf = bytearray()
        self._base = 0          # _buf[0] 在整流中的绝对偏移
        self._on_frame = on_frame
        self._on_error = on_error

    def feed(self, data: bytes):
        self._buf += data
        self._drain(final=False)

    def finish(self):
        """收尾规则：输入结束即所有流结束，残留字节报不完整帧。"""
        self._drain(final=True)
        if not self._buf:
            return
        off = self._base
        if self._buf.startswith(MAGIC) and len(self._buf) >= HEADER_LEN:
            sid = self._buf[2]
            seq = int.from_bytes(self._buf[3:5], "big")
            need = HEADER_LEN + int.from_bytes(self._buf[5:7], "big") + 1
            self._on_error("incomplete_frame", sid, seq, off,
                           "流结束仍有半帧：需 %d 字节，仅存 %d 字节"
                           % (need, len(self._buf)))
        else:
            self._on_error("trailing_bytes", None, None, off,
                           "流结束残留 %d 字节无法构成帧头: %s"
                           % (len(self._buf), bytes(self._buf).hex()))
        self._buf.clear()

    def _drop(self, n: int):
        del self._buf[:n]
        self._base += n

    def _drain(self, final: bool):
        while True:
            idx = self._buf.find(MAGIC)
            if idx < 0:
                # 保留末尾可能是半个 magic 的 0xA5
                keep = 1 if (not final and self._buf.endswith(MAGIC[:1])) else 0
                garbage = len(self._buf) - keep
                if garbage > 0:
                    self._on_error("resync", None, None, self._base,
                                   "丢弃 %d 字节垃圾数据后重新同步: %s"
                                   % (garbage, bytes(self._buf[:garbage]).hex()))
                    self._drop(garbage)
                return
            if idx > 0:
                self._on_error("resync", None, None, self._base,
                               "丢弃 %d 字节垃圾数据后重新同步: %s"
                               % (idx, bytes(self._buf[:idx]).hex()))
                self._drop(idx)
            if len(self._buf) < HEADER_LEN:
                return  # 帧头拆半，等后续分片
            sid = self._buf[2]
            seq = int.from_bytes(self._buf[3:5], "big")
            length = int.from_bytes(self._buf[5:7], "big")
            total = HEADER_LEN + length + 1
            if len(self._buf) < total:
                return  # 帧体拆半，等后续分片
            frame = bytes(self._buf[:total])
            off = self._base
            self._drop(total)
            body = frame[2:-1]      # stream..payload
            want = checksum(body)
            got = frame[-1]
            if got != want:
                self._on_error("checksum_error", sid, seq, off,
                               "期望 0x%02X 实际 0x%02X，载荷已丢弃" % (want, got))
                continue            # 校验失败：不信任载荷，不投递
            self._on_frame(sid, seq, frame[HEADER_LEN:-1], off)


# ---------------------------------------------------------------- 按流重组

class StreamAssembler:
    """单流重组：检测序号缺口与重复，拼接连续报文。"""

    def __init__(self, stream_id: int):
        self.stream_id = stream_id
        self.expected = None      # 下一个期望序号（首帧确定起点）
        self.seen = set()
        self.parts = {}           # seq -> payload

    def add(self, seq, payload, offset, on_error):
        sid = self.stream_id
        if self.expected is None:
            self.expected = seq
        if seq in self.seen:
            on_error("duplicate_seq", sid, seq, offset, "重复序号，已忽略")
            return
        self.seen.add(seq)
        if seq > self.expected:
            missing = list(range(self.expected, seq))
            shown = missing[:MAX_GAP_REPORT]
            detail = "缺失序号 " + ",".join(str(s) for s in shown)
            if len(missing) > len(shown):
                detail += " ... 共 %d 个" % len(missing)
            on_error("seq_gap", sid, seq, offset, detail)
        self.parts[seq] = payload
        self.expected = max(self.expected, seq + 1)

    def message(self) -> bytes:
        """从最小序号起拼接连续部分（缺口处停止）。"""
        if not self.parts:
            return b""
        out = bytearray()
        seq = min(self.parts)
        while seq in self.parts:
            out += self.parts[seq]
            seq += 1
        return bytes(out)

    def contiguous(self) -> bool:
        return bool(self.parts) and \
            len(self.parts) == max(self.parts) - min(self.parts) + 1


# ---------------------------------------------------------------- 驱动与报告

def run(chunks):
    """喂入分片列表，返回 (streams, errors)。"""
    errors = []
    streams = {}

    def on_error(etype, sid, seq, offset, detail):
        errors.append({"type": etype, "stream": sid, "seq": seq,
                       "offset": offset, "detail": detail})

    def on_frame(sid, seq, payload, offset):
        streams.setdefault(sid, StreamAssembler(sid)).add(
            seq, payload, offset, on_error)

    parser = FrameParser(on_frame, on_error)
    for chunk in chunks:
        parser.feed(chunk)
    parser.finish()
    return streams, errors


def render(streams, errors) -> str:
    lines = []
    for sid in sorted(streams):
        asm = streams[sid]
        msg = asm.message()
        lines.append("=== 流 %d ===" % sid)
        lines.append("  报文(hex): %s" % (msg.hex() or "(空)"))
        try:
            text = msg.decode("utf-8")
        except UnicodeDecodeError:
            text = repr(msg)
        lines.append("  报文(text): %s" % text)
        lo, hi = min(asm.parts), max(asm.parts)
        state = "是" if asm.contiguous() else "否（序号 %d..%d 中存在缺口）" % (lo, hi)
        lines.append("  完整: %s" % state)
    if not streams:
        lines.append("（未还原出任何流）")
    lines.append("=== 错误清单 ===")
    if not errors:
        lines.append("（无错误）")
    for i, e in enumerate(errors, 1):
        loc = "offset=0x%04X" % e["offset"]
        target = ""
        if e["stream"] is not None:
            target = " stream=%d" % e["stream"]
        if e["seq"] is not None:
            target += " seq=%d" % e["seq"]
        lines.append("[#%d] %s%s %s: %s"
                     % (i, loc, target, e["type"], e["detail"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 输入与示例

def chunks_from_sizes(blob: bytes, sizes):
    out, pos = [], 0
    i = 0
    while pos < len(blob):
        n = sizes[i % len(sizes)]
        out.append(blob[pos:pos + n])
        pos += n
        i += 1
    return out


def load_chunks(spec: dict):
    if "chunks_hex" in spec:
        return [bytes.fromhex(h) for h in spec["chunks_hex"]]
    if "hex" in spec:
        blob = bytes.fromhex(spec["hex"])
        sizes = spec.get("chunk_sizes")
        return chunks_from_sizes(blob, sizes) if sizes else [blob]
    if "messages" in spec:
        blob = b"".join(
            encode_frame(m["stream"], m["seq"],
                         m.get("hex") and bytes.fromhex(m["hex"])
                         or m.get("text", "").encode("utf-8"))
            for m in spec["messages"])
        sizes = spec.get("chunk_sizes")
        return chunks_from_sizes(blob, sizes) if sizes else [blob]
    raise ValueError("输入需含 chunks_hex / hex / messages 之一")


def build_demo():
    """构造覆盖全部场景的示例流：拆半、粘包、缺口、重复、交错、
    校验错误、结尾半帧。"""
    f_s2_bad = bytearray(encode_frame(2, 1, "世界".encode("utf-8")))
    f_s2_bad[-1] ^= 0xFF                      # 故意破坏校验和
    frames = [
        encode_frame(1, 0, b"Hello, "),
        encode_frame(2, 0, "你好，".encode("utf-8")),   # 跨流交错
        encode_frame(1, 1, b"world!"),
        bytes(f_s2_bad),                                 # 校验错误
        encode_frame(1, 1, b"world!"),                   # 重复序号
        encode_frame(1, 3, b"(after gap)"),              # 缺口：缺 seq 2
        encode_frame(2, 2, "!".encode("utf-8")),        # 缺口：seq 1 被丢弃
    ]
    blob = b"".join(frames)
    blob += encode_frame(3, 0, b"never complete")[:10]   # 结尾半帧
    # 用不均匀分片制造拆半与粘包
    return chunks_from_sizes(blob, [3, 17, 5, 5, 64, 2, 29])


def main(argv=None):
    ap = argparse.ArgumentParser(description="多流字节流报文还原工具")
    ap.add_argument("input", nargs="?", help="输入 JSON 文件（缺省读 stdin）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args(argv)

    if args.demo:
        chunks = build_demo()
    else:
        raw = open(args.input, encoding="utf-8").read() if args.input \
            else sys.stdin.read()
        chunks = load_chunks(json.loads(raw))

    streams, errors = run(chunks)

    if args.json:
        out = {
            "streams": {
                str(sid): {
                    "message_hex": streams[sid].message().hex(),
                    "contiguous": streams[sid].contiguous(),
                } for sid in sorted(streams)
            },
            "errors": errors,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(streams, errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
