#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stream_reassemble.py — 多逻辑连接字节流帧还原工具（纯标准库，单文件）

帧格式（大端）：
    +--------+--------+----------+---------+----------+----------+=========+
    | magic  | stream |   seq    |  length | checksum |  payload ...       |
    | 2 字节 | 1 字节 |  2 字节  | 2 字节  |  1 字节  | length 字节        |
    +--------+--------+----------+---------+----------+----------+=========+
    magic    = 0xAB 0xCD（帧同步头）
    stream   = 流标识 0..255
    seq      = 流内帧序号 0..65535
    length   = payload 字节数（不含头）
    checksum = (stream + seq 高 + seq 低 + length 高 + length 低 + sum(payload)) & 0xFF

能力：
  * 帧按 stream 归组，互不污染（跨流交错安全）
  * length 字段精确切帧；粘包逐帧切出；拆半帧缓存待续（feed 可任意分片）
  * 序号缺口 / 重复序号 / 校验错误，均定位到流与序号
  * 流结束仍有半截帧 -> 报告不完整帧
  * 帧间垃圾字节 -> 报告并自动重新同步到下一个 magic

用法：
    python3 stream_reassemble.py 输入.bin [--chunk N]     # 二进制输入
    python3 stream_reassemble.py 输入.hex --hex           # 十六进制文本输入
    python3 stream_reassemble.py --demo                   # 内置演示（含全部错误场景）

输入示例（hex，两流交错 + 粘包）：
    abcd 0100 0000 0548 656c6c6f 55  abcd 0200 0000 0342 5547 6f ...
"""

import argparse
import sys
from dataclasses import dataclass, field

MAGIC = b"\xAB\xCD"
HEADER_LEN = 8  # magic(2) + stream(1) + seq(2) + length(2) + checksum(1)
MAX_PAYLOAD = 0xFFFF


# ---------------------------------------------------------------- 帧构造（测试/演示用）

def build_frame(stream_id, seq, payload, corrupt_checksum=False):
    """按帧格式构造一帧字节；corrupt_checksum=True 时故意写错校验。"""
    length = len(payload)
    header_wo_magic = bytes([
        stream_id & 0xFF,
        (seq >> 8) & 0xFF, seq & 0xFF,
        (length >> 8) & 0xFF, length & 0xFF,
    ])
    checksum = (sum(header_wo_magic) + sum(payload)) & 0xFF
    if corrupt_checksum:
        checksum ^= 0xFF
    return MAGIC + header_wo_magic + bytes([checksum]) + payload


# ---------------------------------------------------------------- 数据结构

@dataclass
class Error:
    offset: int          # 在原始字节流中的绝对偏移
    stream_id: int       # -1 表示无法确定流
    seq: int             # -1 表示无法确定序号
    kind: str
    detail: str

    def __str__(self):
        where = []
        if self.stream_id >= 0:
            where.append("流%d" % self.stream_id)
        if self.seq >= 0:
            where.append("序号%d" % self.seq)
        loc = (" ".join(where)) if where else "流外"
        return "[偏移 %#06x] %-14s %s : %s" % (self.offset, self.kind, loc, self.detail)


@dataclass
class StreamState:
    messages: list = field(default_factory=list)   # [(seq, payload), ...] 仅收校验通过的帧
    seen: set = field(default_factory=set)         # 已见序号（含校验失败的帧，避免级联误报）
    next_expected: int = None                      # 下一个期望序号


# ---------------------------------------------------------------- 还原器

class Reassembler:
    """增量式帧还原器：任意分片 feed()，最后 finish() 收尾。"""

    def __init__(self):
        self._buf = bytearray()     # 未消费字节（拆半帧缓存于此）
        self._offset = 0            # _buf[0] 在原始流中的绝对偏移
        self.streams = {}           # stream_id -> StreamState
        self.errors = []            # [Error, ...]

    # ---------------- 输入 ----------------

    def feed(self, data: bytes):
        """喂入任意长度的字节块（可整帧、半帧、多帧粘包）。"""
        if data:
            self._buf += data
            self._parse()

    def finish(self):
        """输入结束：缓冲区若仍有残留 -> 报告不完整帧/垃圾字节。"""
        self._parse()
        if not self._buf:
            return
        n = len(self._buf)
        if self._buf.startswith(MAGIC) and n >= 3:
            stream_id = self._buf[2]
            seq = int.from_bytes(self._buf[3:5], "big") if n >= 5 else -1
            need = HEADER_LEN
            if n >= HEADER_LEN:
                need += int.from_bytes(self._buf[5:7], "big")
            self._err(self._offset, stream_id, seq, "不完整帧",
                      "流结束时帧未收齐：已有 %d 字节，整帧需 %d 字节" % (n, need))
        else:
            self._err(self._offset, -1, -1, "垃圾字节",
                      "流结束时残留 %d 字节无法成帧: %s" % (n, self._buf[:16].hex()))
        self._offset += n
        self._buf.clear()

    # ---------------- 解析 ----------------

    def _parse(self):
        while True:
            # 1) 同步到 magic（帧间垃圾 -> 报告并跳过）
            idx = self._buf.find(MAGIC)
            if idx < 0:
                keep = len(MAGIC) - 1                     # 末尾可能是半个 magic
                if len(self._buf) > keep:
                    junk = bytes(self._buf[:-keep])
                    self._err(self._offset, -1, -1, "垃圾字节",
                              "跳过 %d 字节非帧数据: %s%s" % (
                                  len(junk), junk[:16].hex(),
                                  "..." if len(junk) > 16 else ""))
                    del self._buf[:-keep]
                    self._offset += len(junk)
                return
            if idx > 0:
                junk = bytes(self._buf[:idx])
                self._err(self._offset, -1, -1, "垃圾字节",
                          "跳过 %d 字节非帧数据: %s%s" % (
                              len(junk), junk[:16].hex(),
                              "..." if len(junk) > 16 else ""))
                del self._buf[:idx]
                self._offset += idx

            # 2) 头不完整 -> 缓存待续（拆半场景）
            if len(self._buf) < HEADER_LEN:
                return

            stream_id = self._buf[2]
            seq = int.from_bytes(self._buf[3:5], "big")
            length = int.from_bytes(self._buf[5:7], "big")
            checksum = self._buf[7]
            total = HEADER_LEN + length

            # 3) 帧体不完整 -> 缓存待续（拆半场景）
            if len(self._buf) < total:
                return

            # 4) 完整帧切出（粘包时循环继续切下一帧）
            payload = bytes(self._buf[HEADER_LEN:total])
            frame_offset = self._offset
            del self._buf[:total]
            self._offset += total

            calc = (stream_id + (seq >> 8) + (seq & 0xFF)
                    + (length >> 8) + (length & 0xFF) + sum(payload)) & 0xFF
            self._dispatch(stream_id, seq, payload, checksum, calc, frame_offset)

    # ---------------- 按流归组 + 序号/校验检查 ----------------

    def _dispatch(self, stream_id, seq, payload, checksum, calc, offset):
        st = self.streams.setdefault(stream_id, StreamState())

        if checksum != calc:
            self._err(offset, stream_id, seq, "校验错误",
                      "校验和应为 %#04x，帧内为 %#04x，该帧数据已丢弃" % (calc, checksum))
            # 序号仍登记，避免把后续帧误报为缺口
            st.seen.add(seq)
            st.next_expected = max(st.next_expected or 0, seq + 1)
            return

        if seq in st.seen:
            self._err(offset, stream_id, seq, "重复序号",
                      "序号 %d 已接收过，本帧 %d 字节负载被忽略" % (seq, len(payload)))
            return

        if st.next_expected is not None and seq > st.next_expected:
            missing = list(range(st.next_expected, seq))
            shown = ",".join(map(str, missing[:8])) + ("..." if len(missing) > 8 else "")
            self._err(offset, stream_id, seq, "序号缺口",
                      "期望序号 %d，实收 %d，缺失 %d 个序号: %s"
                      % (st.next_expected, seq, len(missing), shown))

        st.seen.add(seq)
        st.next_expected = max(st.next_expected or 0, seq + 1)
        st.messages.append((seq, payload))

    def _err(self, offset, stream_id, seq, kind, detail):
        self.errors.append(Error(offset, stream_id, seq, kind, detail))

    # ---------------- 输出 ----------------

    def report(self, out=sys.stdout):
        print("=" * 64, file=out)
        print("按流还原的报文", file=out)
        print("=" * 64, file=out)
        if not self.streams:
            print("（未解析出任何完整帧）", file=out)
        for sid in sorted(self.streams):
            st = self.streams[sid]
            msgs = sorted(st.messages)
            print("\n--- 流 %d（有效帧 %d 个）---" % (sid, len(msgs)), file=out)
            for seq, payload in msgs:
                try:
                    text = payload.decode("utf-8")
                    shown = repr(text)
                except UnicodeDecodeError:
                    shown = "hex:" + payload.hex()
                print("  [序号 %5d] %4d 字节  %s" % (seq, len(payload), shown), file=out)
            whole = b"".join(p for _, p in msgs)
            try:
                print("  拼接报文: %r" % whole.decode("utf-8"), file=out)
            except UnicodeDecodeError:
                print("  拼接报文(hex): %s" % whole.hex(), file=out)

        print("\n" + "=" * 64, file=out)
        print("错误报告（共 %d 条）" % len(self.errors), file=out)
        print("=" * 64, file=out)
        if not self.errors:
            print("（无错误）", file=out)
        for e in self.errors:
            print("  " + str(e), file=out)


# ---------------------------------------------------------------- 演示

def build_demo_stream():
    """构造一条涵盖全部场景的演示字节流。"""
    s1 = [b"Hello, ", b"world", b"! This ", b"is stream-1."]
    s2 = [b"GET / HTTP/1.1\r\n", b"Host: example\r\n", b"\r\n"]
    frames = []
    # 两流逐帧交错（跨流字节交错）
    frames.append(build_frame(1, 0, s1[0]))
    frames.append(build_frame(2, 0, s2[0]))
    frames.append(build_frame(1, 1, s1[1]))
    frames.append(build_frame(2, 1, s2[1]))
    # 流1 序号缺口：跳过 2，直接来 3
    frames.append(build_frame(1, 3, s1[3]))
    # 流1 重复序号：3 又来一次
    frames.append(build_frame(1, 3, s1[3]))
    # 流2 校验错误帧
    frames.append(build_frame(2, 2, s2[2], corrupt_checksum=True))
    # 帧间垃圾字节
    frames.append(b"\x00\x11\x22GARBAGE")
    # 粘包：流1 补发序号2 + 流2 重发序号2 粘在一起
    frames.append(build_frame(1, 2, s1[2]) + build_frame(2, 2, s2[2]))
    # 结尾：流2 来了一个拆半就断掉的帧（不完整帧）
    frames.append(build_frame(2, 3, b"truncated-body")[:9])
    return b"".join(frames)


def run_demo():
    data = build_demo_stream()
    print("演示输入：%d 字节，以 7 字节为单位分片喂入（模拟拆半/粘包）\n" % len(data))
    r = Reassembler()
    for i in range(0, len(data), 7):          # 任意小分片 -> 必然出现拆半
        r.feed(data[i:i + 7])
    r.finish()
    r.report()


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="多逻辑连接字节流帧还原工具（帧格式见文件头注释）")
    ap.add_argument("input", nargs="?", help="输入字节流文件（默认二进制）")
    ap.add_argument("--hex", action="store_true",
                    help="输入为十六进制文本（空白分隔）而非二进制")
    ap.add_argument("--chunk", type=int, default=0, metavar="N",
                    help="按每 N 字节分片喂入，模拟拆半/粘包（默认整段一次喂入）")
    ap.add_argument("--demo", action="store_true", help="运行内置演示")
    args = ap.parse_args(argv)

    if args.demo:
        run_demo()
        return 0
    if not args.input:
        ap.error("需要输入文件，或使用 --demo 运行演示")

    raw = open(args.input, "rb").read()
    if args.hex:
        data = bytes.fromhex(raw.decode("ascii"))
    else:
        data = raw

    r = Reassembler()
    if args.chunk > 0:
        for i in range(0, len(data), args.chunk):
            r.feed(data[i:i + args.chunk])
    else:
        r.feed(data)
    r.finish()
    r.report()
    return 1 if r.errors else 0


if __name__ == "__main__":
    sys.exit(main())
