#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Verilog/FPGA Video Pipeline Debug Agent
--------------------------------------

功能：
1. 扫描 Verilog/SystemVerilog 工程文件；
2. 自动提取 module、端口、例化关系、时钟/复位/视频信号；
3. 生成模块层次结构；
4. 检查常见风险：
   - 顶层模块是否存在；
   - 例化端口是否空连接；
   - 时钟/复位信号识别；
   - 复位极性是否可能写反；
   - 一个 always 中是否出现多个时钟边沿；
   - 时序 always 中是否疑似使用阻塞赋值；
   - 组合 always 中是否疑似使用非阻塞赋值；
   - 同一信号是否被多个 assign 驱动；
   - 视频链路中 data/vs/hs/de/valid/frame 等信号识别；
5. 输出 Markdown 分析报告和 Graphviz DOT 层次图。

使用示例：
    python verilog_agent.py --root ./rtl --top top --out report.md

追踪某个信号：
    python verilog_agent.py --root ./rtl --top top --trace frame_stable --out report.md

说明：
本工具是静态分析工具，不替代仿真、综合和时序分析。
它适合在调试 MIPI/CSI/DSI/HDMI 视频链路时快速梳理工程结构。
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Iterable


# ============================================================
# 1. 数据结构
# ============================================================

@dataclasses.dataclass
class Port:
    name: str
    direction: str = "unknown"
    width: str = ""
    raw: str = ""


@dataclasses.dataclass
class SignalDecl:
    name: str
    kind: str = "wire"
    width: str = ""
    raw: str = ""


@dataclasses.dataclass
class Instance:
    module_type: str
    instance_name: str
    port_map: Dict[str, str]
    raw: str = ""
    file: str = ""
    line: int = 0


@dataclasses.dataclass
class AlwaysBlock:
    sensitivity: str
    body: str
    file: str = ""
    line: int = 0


@dataclasses.dataclass
class ModuleDef:
    name: str
    file: str
    start_line: int
    header: str = ""
    body: str = ""
    ports: Dict[str, Port] = dataclasses.field(default_factory=dict)
    signals: Dict[str, SignalDecl] = dataclasses.field(default_factory=dict)
    parameters: Dict[str, str] = dataclasses.field(default_factory=dict)
    instances: List[Instance] = dataclasses.field(default_factory=list)
    always_blocks: List[AlwaysBlock] = dataclasses.field(default_factory=list)
    assigns: List[Tuple[str, str, int]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Finding:
    level: str
    title: str
    detail: str
    file: str = ""
    line: int = 0


@dataclasses.dataclass
class ProjectDB:
    root: Path
    modules: Dict[str, ModuleDef] = dataclasses.field(default_factory=dict)
    files: List[Path] = dataclasses.field(default_factory=list)
    findings: List[Finding] = dataclasses.field(default_factory=list)


# ============================================================
# 2. 关键词规则
# ============================================================

VERILOG_EXTS = {".v", ".sv", ".vh", ".svh"}

CLOCK_PATTERNS = [
    r"\bclk\b",
    r"clock",
    r"pclk",
    r"pix_clk",
    r"pixel_clk",
    r"sysclk",
    r"i_sysclk",
    r"mipi.*clk",
    r"hdmi.*clk",
    r"tx.*clk",
    r"rx.*clk",
    r"ck0",
    r"slow_clk",
]

RESET_PATTERNS = [
    r"\brst\b",
    r"reset",
    r"rst_n",
    r"aresetn",
    r"sys_rst_n",
    r"reset_n",
]

VIDEO_PATTERNS = {
    "vs": [
        r"\bvs\b",
        r"v_sync",
        r"vsync",
        r"raw_vs",
        r"rgb_vs",
        r"video_vs",
        r"i_vs",
    ],
    "hs": [
        r"\bhs\b",
        r"h_sync",
        r"hsync",
        r"raw_hs",
        r"rgb_hs",
        r"video_hs",
        r"i_hs",
    ],
    "de": [
        r"\bde\b",
        r"data_en",
        r"den",
        r"raw_de",
        r"rgb_de",
        r"video_de",
        r"i_de",
    ],
    "valid": [
        r"valid",
        r"raw_valid",
        r"rgb_valid",
        r"fifo_wr_en",
    ],
    "data": [
        r"data",
        r"vin",
        r"pixel",
        r"rgb",
        r"raw",
        r"ycbcr",
        r"tmds",
    ],
    "frame": [
        r"frame",
        r"frame_start",
        r"frame_stable",
        r"frame_pix_num",
    ],
    "i2c": [
        r"scl",
        r"sda",
        r"i2c",
    ],
    "mipi": [
        r"mipi",
        r"dphy",
        r"hs_ena",
        r"hs_term",
        r"\blp\b",
    ],
    "hdmi": [
        r"hdmi",
        r"tmds",
        r"dvi",
    ],
}


# ============================================================
# 3. 基础工具函数
# ============================================================

def read_text(path: Path) -> str:
    """
    兼容常见编码读取源码。
    """
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="ignore")


def strip_comments_keep_lines(text: str) -> str:
    """
    删除 Verilog 注释，但尽量保留行号。
    """
    def repl_block(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    text = re.sub(r"/\*.*?\*/", repl_block, text, flags=re.S)
    text = re.sub(r"//.*", "", text)
    return text


def line_of_pos(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def name_matches(name: str, patterns: Iterable[str]) -> bool:
    low = name.lower()
    return any(re.search(p, low, re.I) for p in patterns)


def split_top_level_commas(s: str) -> List[str]:
    """
    按顶层逗号拆分，避免拆开函数参数、拼接符号、位宽等内部逗号。
    """
    parts = []
    buf = []

    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0

    for ch in s:
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)

        if ch == "," and depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(ch)

    last = "".join(buf).strip()
    if last:
        parts.append(last)

    return parts


def find_matching_endmodule(text: str, start_pos: int) -> int:
    m = re.search(r"\bendmodule\b", text[start_pos:], flags=re.I)
    if not m:
        return len(text)
    return start_pos + m.end()


def balanced_extract(
    text: str,
    open_pos: int,
    open_ch: str = "(",
    close_ch: str = ")",
) -> Tuple[str, int]:
    """
    从 open_pos 位置的左括号开始，提取平衡括号内的内容。
    """
    if open_pos >= len(text) or text[open_pos] != open_ch:
        return "", open_pos

    depth = 0
    start = open_pos + 1
    i = open_pos

    while i < len(text):
        ch = text[i]

        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i], i

        i += 1

    return text[start:], len(text) - 1


# ============================================================
# 4. Verilog 解析器
# ============================================================

class VerilogParser:
    def __init__(self, root: Path):
        self.root = root

    def scan_files(self) -> List[Path]:
        files = []
        for p in self.root.rglob("*"):
            if p.is_file() and p.suffix.lower() in VERILOG_EXTS:
                files.append(p)
        return sorted(files)

    def parse_project(self) -> ProjectDB:
        db = ProjectDB(root=self.root)
        db.files = self.scan_files()

        for f in db.files:
            text_raw = read_text(f)
            text = strip_comments_keep_lines(text_raw)

            for mdef in self.parse_modules_in_file(f, text):
                if mdef.name in db.modules:
                    old = db.modules[mdef.name]
                    db.findings.append(
                        Finding(
                            level="WARN",
                            title="发现同名模块定义",
                            detail=(
                                f"模块 {mdef.name} 在多个文件中定义："
                                f"{old.file} 与 {mdef.file}。"
                                f"请确认是否为重复文件或版本冲突。"
                            ),
                            file=str(f),
                            line=mdef.start_line,
                        )
                    )

                db.modules[mdef.name] = mdef

        self.post_check_undefined_instances(db)
        return db

    def parse_modules_in_file(self, path: Path, text: str) -> List[ModuleDef]:
        modules: List[ModuleDef] = []
        module_re = re.compile(r"\bmodule\s+([A-Za-z_][\w$]*)\b", re.I)

        pos = 0
        while True:
            m = module_re.search(text, pos)

            if not m:
                break

            name = m.group(1)
            start = m.start()
            end = find_matching_endmodule(text, m.end())
            chunk = text[start:end]
            start_line = line_of_pos(text, start)

            mdef = ModuleDef(
                name=name,
                file=str(path),
                start_line=start_line,
                body=chunk,
            )

            self.parse_module_header(mdef, chunk)
            self.parse_parameters(mdef, chunk)
            self.parse_declarations(mdef, chunk)
            self.parse_assigns(mdef, chunk)
            self.parse_always_blocks(mdef, chunk)
            self.parse_instances(mdef, chunk)

            modules.append(mdef)
            pos = end

        return modules

    def parse_module_header(self, mdef: ModuleDef, chunk: str) -> None:
        header_end = chunk.find(";")
        if header_end < 0:
            mdef.header = chunk[:300]
            return

        header = chunk[:header_end + 1]
        mdef.header = header

        paren_positions = [i for i, ch in enumerate(header) if ch == "("]
        if not paren_positions:
            return

        port_text = ""

        for p in reversed(paren_positions):
            candidate, close_pos = balanced_extract(header, p)
            if close_pos < len(header) and ";" in header[close_pos:]:
                port_text = candidate
                break

        if not port_text:
            return

        last_dir = "unknown"
        last_width = ""

        for item in split_top_level_commas(port_text):
            raw = norm_space(item)
            if not raw:
                continue

            parsed = self.parse_port_item(raw, last_dir, last_width)

            if parsed:
                port = parsed

                if port.direction != "unknown":
                    last_dir = port.direction

                if port.width:
                    last_width = port.width

                mdef.ports[port.name] = port

    def parse_port_item(
        self,
        raw: str,
        last_dir: str,
        last_width: str,
    ) -> Optional[Port]:
        s = raw.strip().rstrip(");")
        s = re.sub(r"=.*$", "", s).strip()

        tokens = s.split()

        if not tokens:
            return None

        direction = "unknown"

        if tokens[0] in {"input", "output", "inout"}:
            direction = tokens.pop(0)
        else:
            direction = last_dir

        tokens = [
            t for t in tokens
            if t not in {"wire", "reg", "logic", "signed", "unsigned"}
        ]

        width = ""

        width_match = re.search(r"(\[[^\]]+\])", s)

        if width_match:
            width = width_match.group(1)
        elif direction == "unknown":
            width = last_width

        names = re.findall(r"[A-Za-z_][\w$]*", s)
        names = [
            n for n in names
            if n not in {
                "input",
                "output",
                "inout",
                "wire",
                "reg",
                "logic",
                "signed",
                "unsigned",
            }
        ]

        if not names:
            return None

        name = names[-1]

        return Port(
            name=name,
            direction=direction,
            width=width,
            raw=raw,
        )

    def parse_parameters(self, mdef: ModuleDef, chunk: str) -> None:
        for pm in re.finditer(r"\bparameter\s+([^;]+);", chunk, flags=re.I | re.S):
            body = pm.group(1)

            for item in split_top_level_commas(body):
                mm = re.match(
                    r"\s*([A-Za-z_][\w$]*)\s*=\s*(.+)\s*$",
                    item,
                    flags=re.S,
                )

                if mm:
                    mdef.parameters[mm.group(1)] = norm_space(mm.group(2))

    def parse_declarations(self, mdef: ModuleDef, chunk: str) -> None:
        decl_re = re.compile(
            r"\b(input|output|inout|wire|reg|logic)\b\s+([^;]+);",
            re.I | re.S,
        )

        for dm in decl_re.finditer(chunk):
            kind = dm.group(1).lower()
            rest = dm.group(2)
            raw = norm_space(dm.group(0))

            width_match = re.search(r"(\[[^\]]+\])", rest)
            width = width_match.group(1) if width_match else ""

            cleaned = re.sub(r"\[[^\]]+\]", " ", rest)
            cleaned = re.sub(
                r"\b(reg|wire|logic|signed|unsigned)\b",
                " ",
                cleaned,
                flags=re.I,
            )

            for item in split_top_level_commas(cleaned):
                item = re.sub(r"=.*$", "", item).strip()
                nm = re.search(r"([A-Za-z_][\w$]*)\s*$", item)

                if not nm:
                    continue

                name = nm.group(1)

                if kind in {"input", "output", "inout"}:
                    old = mdef.ports.get(name)

                    if old:
                        old.direction = kind
                        old.width = old.width or width
                        old.raw = old.raw or raw
                    else:
                        mdef.ports[name] = Port(
                            name=name,
                            direction=kind,
                            width=width,
                            raw=raw,
                        )
                else:
                    mdef.signals[name] = SignalDecl(
                        name=name,
                        kind=kind,
                        width=width,
                        raw=raw,
                    )

    def parse_assigns(self, mdef: ModuleDef, chunk: str) -> None:
        for am in re.finditer(
            r"\bassign\s+(.+?)\s*=\s*(.+?);",
            chunk,
            flags=re.I | re.S,
        ):
            lhs = norm_space(am.group(1))
            rhs = norm_space(am.group(2))
            line = mdef.start_line + line_of_pos(chunk, am.start()) - 1
            mdef.assigns.append((lhs, rhs, line))

    def parse_always_blocks(self, mdef: ModuleDef, chunk: str) -> None:
        always_re = re.compile(
            r"\balways(?:_ff|_comb|_latch)?\s*@?\s*(?:\((.*?)\))?",
            re.I | re.S,
        )

        matches = list(always_re.finditer(chunk))

        for idx, am in enumerate(matches):
            start = am.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(chunk)
            block = chunk[start:end]
            sensitivity = norm_space(am.group(1) or "")
            line = mdef.start_line + line_of_pos(chunk, start) - 1

            mdef.always_blocks.append(
                AlwaysBlock(
                    sensitivity=sensitivity,
                    body=block,
                    file=mdef.file,
                    line=line,
                )
            )

    def parse_instances(self, mdef: ModuleDef, chunk: str) -> None:
        body = chunk
        first_semicolon = body.find(";")

        if first_semicolon >= 0:
            body = body[first_semicolon + 1:]

        inst_re = re.compile(
            r"\b([A-Za-z_][\w$]*)\b\s*"
            r"(?:#\s*\((.*?)\)\s*)?"
            r"\b([A-Za-z_][\w$]*)\b\s*"
            r"\((.*?)\)\s*;",
            re.I | re.S,
        )

        keywords = {
            "module",
            "if",
            "for",
            "while",
            "case",
            "casex",
            "casez",
            "assign",
            "always",
            "begin",
            "end",
            "function",
            "task",
            "generate",
            "initial",
            "else",
            "repeat",
        }

        for im in inst_re.finditer(body):
            module_type = im.group(1)
            instance_name = im.group(3)
            port_blob = im.group(4)

            if module_type.lower() in keywords:
                continue

            if instance_name.lower() in keywords:
                continue

            if module_type.lower() in {"input", "output", "wire", "reg", "logic"}:
                continue

            port_map = self.parse_port_map(port_blob)

            if not port_map:
                continue

            raw = norm_space(im.group(0))
            line = mdef.start_line + line_of_pos(
                chunk,
                first_semicolon + 1 + im.start(),
            ) - 1

            mdef.instances.append(
                Instance(
                    module_type=module_type,
                    instance_name=instance_name,
                    port_map=port_map,
                    raw=raw,
                    file=mdef.file,
                    line=line,
                )
            )

    def parse_port_map(self, blob: str) -> Dict[str, str]:
        port_map: Dict[str, str] = {}

        named = re.findall(
            r"\.\s*([A-Za-z_][\w$]*)\s*\((.*?)\)",
            blob,
            flags=re.S,
        )

        if named:
            for p, sig in named:
                port_map[p] = norm_space(sig)

            return port_map

        parts = split_top_level_commas(blob)

        for i, sig in enumerate(parts):
            port_map[f"_pos_{i}"] = norm_space(sig)

        return port_map

    def post_check_undefined_instances(self, db: ProjectDB) -> None:
        known = set(db.modules.keys())

        for m in db.modules.values():
            for inst in m.instances:
                if inst.module_type not in known:
                    db.findings.append(
                        Finding(
                            level="INFO",
                            title="例化模块未在当前工程中找到定义",
                            detail=(
                                f"{m.name} 例化了 {inst.module_type} "
                                f"({inst.instance_name})。"
                                f"如果这是厂商 IP 或黑盒模块，可忽略；"
                                f"否则需要确认文件是否加入工程。"
                            ),
                            file=inst.file,
                            line=inst.line,
                        )
                    )


# ============================================================
# 5. Agent 分析器
# ============================================================

class VerilogDebugAgent:
    def __init__(self, db: ProjectDB, top: Optional[str] = None):
        self.db = db
        self.top = top or self.guess_top()
        self.children: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        self.parents: Dict[str, List[str]] = defaultdict(list)

        self.build_hierarchy()

    def guess_top(self) -> Optional[str]:
        if not self.db.modules:
            return None

        instantiated = {
            inst.module_type
            for m in self.db.modules.values()
            for inst in m.instances
        }

        candidates = [
            name for name in self.db.modules
            if name not in instantiated
        ]

        for prefer in [
            "top",
            "system_top",
            "fpga_top",
            "hdmi_top",
            "dsi_tx_top",
            "mipi_top",
        ]:
            for c in candidates or self.db.modules.keys():
                if c.lower() == prefer:
                    return c

        if candidates:
            return candidates[0]

        return next(iter(self.db.modules.keys()))

    def build_hierarchy(self) -> None:
        for parent, m in self.db.modules.items():
            for inst in m.instances:
                self.children[parent].append(
                    (inst.module_type, inst.instance_name)
                )
                self.parents[inst.module_type].append(parent)

    def analyze(self, trace_signal: Optional[str] = None) -> None:
        self.check_top_exists()
        self.check_unconnected_ports()
        self.check_clock_reset()
        self.check_video_signals()
        self.check_assign_drivers()
        self.check_always_style()
        self.check_todo_fixme()

        if trace_signal:
            self.trace_signal(trace_signal)

    def check_top_exists(self) -> None:
        if not self.top:
            self.db.findings.append(
                Finding(
                    level="RISK",
                    title="未找到顶层模块",
                    detail="工程中没有解析到任何模块。",
                )
            )

        elif self.top not in self.db.modules:
            self.db.findings.append(
                Finding(
                    level="RISK",
                    title="指定顶层模块不存在",
                    detail=f"--top={self.top} 不在已解析模块中。请检查顶层模块名。",
                )
            )

    def check_unconnected_ports(self) -> None:
        for m in self.db.modules.values():
            for inst in m.instances:
                for p, sig in inst.port_map.items():
                    if sig.strip() == "":
                        self.db.findings.append(
                            Finding(
                                level="WARN",
                                title="例化端口未连接",
                                detail=(
                                    f"{m.name}.{inst.instance_name} 的端口 {p} "
                                    f"为空连接。若非刻意悬空，可能导致功能异常。"
                                ),
                                file=inst.file,
                                line=inst.line,
                            )
                        )

    def check_clock_reset(self) -> None:
        for m in self.db.modules.values():
            ports_and_sigs = {
                **m.ports,
                **m.signals,
            }

            clock_names = [
                n for n in ports_and_sigs
                if name_matches(n, CLOCK_PATTERNS)
            ]

            reset_names = [
                n for n in ports_and_sigs
                if name_matches(n, RESET_PATTERNS)
            ]

            if clock_names:
                self.db.findings.append(
                    Finding(
                        level="INFO",
                        title="识别到时钟信号",
                        detail=(
                            f"模块 {m.name} 中可能的时钟信号："
                            f"{', '.join(clock_names[:12])}"
                            f"{' ...' if len(clock_names) > 12 else ''}"
                        ),
                        file=m.file,
                        line=m.start_line,
                    )
                )

            if reset_names:
                self.db.findings.append(
                    Finding(
                        level="INFO",
                        title="识别到复位信号",
                        detail=(
                            f"模块 {m.name} 中可能的复位信号："
                            f"{', '.join(reset_names[:12])}"
                            f"{' ...' if len(reset_names) > 12 else ''}"
                        ),
                        file=m.file,
                        line=m.start_line,
                    )
                )

            for ab in m.always_blocks:
                sens = ab.sensitivity.lower()

                if "posedge" in sens or "negedge" in sens:
                    edges = re.findall(
                        r"(posedge|negedge)\s+([A-Za-z_][\w$]*)",
                        sens,
                    )

                    clk_edges = [
                        (e, s) for e, s in edges
                        if name_matches(s, CLOCK_PATTERNS)
                    ]

                    rst_edges = [
                        (e, s) for e, s in edges
                        if name_matches(s, RESET_PATTERNS)
                    ]

                    if len(clk_edges) > 1:
                        self.db.findings.append(
                            Finding(
                                level="RISK",
                                title="一个时序 always 中出现多个时钟边沿",
                                detail=(
                                    f"模块 {m.name} 的 always 敏感列表为 "
                                    f"({ab.sensitivity})。多个时钟边沿通常不可综合"
                                    f"或存在 CDC 风险。"
                                ),
                                file=ab.file,
                                line=ab.line,
                            )
                        )

                    for edge, rst in rst_edges:
                        if rst.endswith("_n") and edge != "negedge":
                            self.db.findings.append(
                                Finding(
                                    level="WARN",
                                    title="低有效复位疑似使用了 posedge",
                                    detail=(
                                        f"复位信号 {rst} 看起来是低有效，"
                                        f"但敏感边沿是 {edge}。"
                                        f"请确认复位极性是否正确。"
                                    ),
                                    file=ab.file,
                                    line=ab.line,
                                )
                            )

                        if (
                            not rst.endswith("_n")
                            and edge != "posedge"
                            and "reset_n" not in rst
                        ):
                            self.db.findings.append(
                                Finding(
                                    level="WARN",
                                    title="高有效复位疑似使用了 negedge",
                                    detail=(
                                        f"复位信号 {rst} 看起来不是低有效命名，"
                                        f"但敏感边沿是 {edge}。"
                                        f"请确认复位极性是否正确。"
                                    ),
                                    file=ab.file,
                                    line=ab.line,
                                )
                            )

    def check_video_signals(self) -> None:
        for m in self.db.modules.values():
            names = list(m.ports.keys()) + list(m.signals.keys())
            found: Dict[str, List[str]] = {}

            for kind, patterns in VIDEO_PATTERNS.items():
                matched = [
                    n for n in names
                    if name_matches(n, patterns)
                ]

                if matched:
                    found[kind] = matched

            if found:
                msg_lines = [
                    f"模块 {m.name} 中识别到视频/接口相关信号："
                ]

                for k, vals in found.items():
                    msg_lines.append(
                        f"- {k}: {', '.join(vals[:10])}"
                        f"{' ...' if len(vals) > 10 else ''}"
                    )

                self.db.findings.append(
                    Finding(
                        level="INFO",
                        title="视频链路信号识别",
                        detail="\n".join(msg_lines),
                        file=m.file,
                        line=m.start_line,
                    )
                )

            data_like = found.get("data", [])
            has_sync = any(
                k in found
                for k in ["vs", "hs", "de", "valid"]
            )

            if data_like and not has_sync:
                self.db.findings.append(
                    Finding(
                        level="WARN",
                        title="数据通道缺少同步/有效信号",
                        detail=(
                            f"模块 {m.name} 存在数据类信号，"
                            f"但未识别到 vs/hs/de/valid。"
                            f"若该模块属于视频链路，请确认同步信号是否遗漏"
                            f"或命名特殊。"
                        ),
                        file=m.file,
                        line=m.start_line,
                    )
                )

    def check_assign_drivers(self) -> None:
        for m in self.db.modules.values():
            drivers: Dict[str, List[int]] = defaultdict(list)

            for lhs, _rhs, line in m.assigns:
                sig = self.extract_lhs_signal(lhs)

                if sig:
                    drivers[sig].append(line)

            for sig, lines in drivers.items():
                if len(lines) > 1:
                    self.db.findings.append(
                        Finding(
                            level="WARN",
                            title="同一信号存在多个连续赋值驱动",
                            detail=(
                                f"模块 {m.name} 中信号 {sig} 被多个 assign 驱动，"
                                f"位置行号：{lines}。"
                                f"请确认是否存在多驱动冲突。"
                            ),
                            file=m.file,
                            line=lines[0],
                        )
                    )

    def extract_lhs_signal(self, lhs: str) -> Optional[str]:
        m = re.match(r"\s*([A-Za-z_][\w$]*)", lhs)
        return m.group(1) if m else None

    def check_always_style(self) -> None:
        for m in self.db.modules.values():
            for ab in m.always_blocks:
                body = ab.body
                sens = ab.sensitivity.lower()

                is_seq = "posedge" in sens or "negedge" in sens
                has_blocking = bool(
                    re.search(r"(?<![<>=!])=(?!=)", body)
                )
                has_nonblocking = "<=" in body

                if is_seq and has_blocking:
                    self.db.findings.append(
                        Finding(
                            level="WARN",
                            title="时序 always 中疑似使用阻塞赋值",
                            detail=(
                                f"模块 {m.name} 的时序 always 中检测到 '='。"
                                f"时序寄存器逻辑通常建议使用 '<='。"
                            ),
                            file=ab.file,
                            line=ab.line,
                        )
                    )

                if not is_seq and has_nonblocking:
                    self.db.findings.append(
                        Finding(
                            level="WARN",
                            title="组合 always 中疑似使用非阻塞赋值",
                            detail=(
                                f"模块 {m.name} 的组合 always 中检测到 '<='。"
                                f"组合逻辑通常建议使用 '='，"
                                f"请确认是否符合设计意图。"
                            ),
                            file=ab.file,
                            line=ab.line,
                        )
                    )

    def check_todo_fixme(self) -> None:
        for p in self.db.files:
            raw = read_text(p)

            for idx, line in enumerate(raw.splitlines(), 1):
                if re.search(r"TODO|FIXME|待修改|临时|debug", line, re.I):
                    self.db.findings.append(
                        Finding(
                            level="INFO",
                            title="源码中存在 TODO/FIXME/debug 标记",
                            detail=norm_space(line),
                            file=str(p),
                            line=idx,
                        )
                    )

    def trace_signal(self, signal: str) -> None:
        hits = []

        for m in self.db.modules.values():
            if signal in m.ports or signal in m.signals:
                hits.append(
                    f"模块 {m.name} 内部定义/端口中存在 {signal}"
                )

            for lhs, rhs, line in m.assigns:
                if (
                    re.search(rf"\b{re.escape(signal)}\b", lhs)
                    or re.search(rf"\b{re.escape(signal)}\b", rhs)
                ):
                    hits.append(
                        f"模块 {m.name} 第 {line} 行 assign 中出现："
                        f"{lhs} = {rhs}"
                    )

            for inst in m.instances:
                for p, sig in inst.port_map.items():
                    if re.search(rf"\b{re.escape(signal)}\b", sig):
                        hits.append(
                            f"模块 {m.name} 中例化 {inst.module_type} "
                            f"{inst.instance_name} 的端口 .{p}({sig}) "
                            f"使用了 {signal}"
                        )

        if hits:
            self.db.findings.append(
                Finding(
                    level="INFO",
                    title=f"信号追踪：{signal}",
                    detail=(
                        "\n".join(f"- {h}" for h in hits[:80])
                        + ("\n- ..." if len(hits) > 80 else "")
                    ),
                )
            )
        else:
            self.db.findings.append(
                Finding(
                    level="WARN",
                    title=f"信号追踪：{signal}",
                    detail=(
                        f"没有在工程中找到信号 {signal}。"
                        f"请检查大小写或命名。"
                    ),
                )
            )

    def get_hierarchy_lines(self) -> List[str]:
        if not self.top or self.top not in self.db.modules:
            return ["未找到有效顶层模块。"]

        lines: List[str] = []
        visited: Set[str] = set()

        def dfs(mod: str, prefix: str = "") -> None:
            if mod in visited:
                lines.append(prefix + f"{mod} <递归/重复引用，已省略>")
                return

            visited.add(mod)
            lines.append(prefix + mod)

            for child_type, inst_name in self.children.get(mod, []):
                mark = "" if child_type in self.db.modules else " [blackbox/IP?]"
                lines.append(
                    prefix + f"  ├─ {inst_name}: {child_type}{mark}"
                )

                if child_type in self.db.modules:
                    dfs(child_type, prefix + "  │  ")

            visited.remove(mod)

        dfs(self.top)
        return lines

    def build_dot(self) -> str:
        lines = [
            "digraph VerilogHierarchy {",
            "  rankdir=LR;",
            "  node [shape=box];",
        ]

        for parent, childs in self.children.items():
            for child_type, inst_name in childs:
                label = f"{inst_name}\\n{child_type}"

                if child_type in self.db.modules:
                    lines.append(
                        f'  "{parent}" -> "{child_type}" '
                        f'[label="{inst_name}"];'
                    )
                else:
                    ip_node = f"{child_type}::{inst_name}"
                    lines.append(
                        f'  "{ip_node}" [style=dashed,label="{label}"];'
                    )
                    lines.append(
                        f'  "{parent}" -> "{ip_node}";'
                    )

        lines.append("}")
        return "\n".join(lines)


# ============================================================
# 6. 报告生成器
# ============================================================

class ReportWriter:
    def __init__(self, db: ProjectDB, agent: VerilogDebugAgent):
        self.db = db
        self.agent = agent

    def write_markdown(self, out_path: Path) -> None:
        md = self.render_markdown()
        out_path.write_text(md, encoding="utf-8")

    def render_markdown(self) -> str:
        lines: List[str] = []

        lines.append("# Verilog/FPGA 视频链路调试 Agent 分析报告")
        lines.append("")

        lines.append("## 1. 工程概况")
        lines.append("")
        lines.append(f"- 工程路径：`{self.db.root}`")
        lines.append(f"- Verilog/SystemVerilog 文件数：{len(self.db.files)}")
        lines.append(f"- 解析到模块数：{len(self.db.modules)}")
        lines.append(f"- 推定/指定顶层模块：`{self.agent.top}`")
        lines.append("")

        lines.append("## 2. 模块层次结构")
        lines.append("")
        lines.append("```text")
        lines.extend(self.agent.get_hierarchy_lines())
        lines.append("```")
        lines.append("")

        lines.append("## 3. 模块摘要")
        lines.append("")
        lines.append("| 模块 | 文件 | 端口数 | 内部信号数 | 例化数 | 参数数 |")
        lines.append("|---|---|---:|---:|---:|---:|")

        for name, m in sorted(self.db.modules.items()):
            lines.append(
                f"| `{name}` | `{self.rel(m.file)}` | "
                f"{len(m.ports)} | {len(m.signals)} | "
                f"{len(m.instances)} | {len(m.parameters)} |"
            )

        lines.append("")

        lines.append("## 4. 关键接口与视频信号摘要")
        lines.append("")

        section_id = 1

        for name, m in sorted(self.db.modules.items()):
            sigs = self.collect_key_signals(m)

            if not any(sigs.values()):
                continue

            lines.append(f"### 4.{section_id} `{name}`")
            lines.append("")

            section_id += 1

            for kind, vals in sigs.items():
                if vals:
                    lines.append(
                        f"- {kind}：`"
                        + "`, `".join(vals[:20])
                        + "`"
                        + (" ..." if len(vals) > 20 else "")
                    )

            lines.append("")

        lines.append("## 5. 风险检查结果")
        lines.append("")

        if not self.db.findings:
            lines.append("未发现明显风险。")
        else:
            grouped: Dict[str, List[Finding]] = defaultdict(list)

            for f in self.db.findings:
                grouped[f.level].append(f)

            for level in ["RISK", "WARN", "INFO"]:
                items = grouped.get(level, [])

                if not items:
                    continue

                title = {
                    "RISK": "高风险",
                    "WARN": "警告",
                    "INFO": "提示",
                }[level]

                lines.append(f"### {title}（{len(items)} 项）")
                lines.append("")

                for i, f in enumerate(items, 1):
                    loc = ""

                    if f.file:
                        loc = f" `{self.rel(f.file)}`"

                        if f.line:
                            loc += f":{f.line}"

                    lines.append(f"**{i}. {f.title}**{loc}")
                    lines.append("")
                    lines.append(f.detail.replace("\n", "\n\n"))
                    lines.append("")

        lines.append("## 6. 调试建议")
        lines.append("")
        lines.append(
            "1. 如果 HDMI 输出黑屏，优先检查：PLL locked、复位释放、"
            "`i_vs/i_hs/i_de`、RGB 数据是否有效、`frame_stable` 是否长期为 1。"
        )
        lines.append(
            "2. 如果画面撕裂或偏移，优先检查：`vs/hs/de` 与像素数据是否同拍、"
            "FIFO 写读时钟域是否正确、帧起始 `frame_start` 是否准确。"
        )
        lines.append(
            "3. 如果 MIPI 输入异常，优先检查：D-PHY lane 使能、HS/LP 状态切换、"
            "I2C 初始化是否完成、sensor 输出分辨率和后级时序是否一致。"
        )
        lines.append(
            "4. 如果固定视频输出正常但摄像头输入异常，说明 HDMI 后级大概率可用，"
            "应重点检查 MIPI RX、Debayer、颜色空间转换和数据位宽对齐。"
        )
        lines.append(
            "5. 本报告只做静态检查，最终仍需要结合仿真波形、ILA/SignalTap 抓信号"
            "和综合时序报告验证。"
        )
        lines.append("")

        return "\n".join(lines)

    def collect_key_signals(self, m: ModuleDef) -> Dict[str, List[str]]:
        names = list(m.ports.keys()) + list(m.signals.keys())

        out: Dict[str, List[str]] = {}

        out["clock"] = [
            n for n in names
            if name_matches(n, CLOCK_PATTERNS)
        ]

        out["reset"] = [
            n for n in names
            if name_matches(n, RESET_PATTERNS)
        ]

        for kind, patterns in VIDEO_PATTERNS.items():
            out[kind] = [
                n for n in names
                if name_matches(n, patterns)
            ]

        return out

    def rel(self, p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(self.db.root.resolve()))
        except Exception:
            return str(p)


# ============================================================
# 7. CLI 入口
# ============================================================

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verilog/FPGA 视频链路调试 Agent："
            "解析模块层次、端口连接、时钟复位和视频信号。"
        )
    )

    parser.add_argument(
        "--root",
        required=True,
        help="Verilog 工程根目录，例如 ./rtl 或 ./project",
    )

    parser.add_argument(
        "--top",
        default=None,
        help="顶层模块名，例如 top、hdmi_top、system_top。不填则自动猜测。",
    )

    parser.add_argument(
        "--out",
        default="verilog_agent_report.md",
        help="输出 Markdown 报告路径",
    )

    parser.add_argument(
        "--dot",
        default="verilog_hierarchy.dot",
        help="输出 Graphviz DOT 层次图路径",
    )

    parser.add_argument(
        "--trace",
        default=None,
        help="可选：追踪某个信号名，例如 i_de、frame_stable、sys_rst_n",
    )

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    root = Path(args.root).expanduser().resolve()

    if not root.exists():
        print(f"[ERROR] 工程路径不存在：{root}", file=sys.stderr)
        return 2

    if not root.is_dir():
        print(f"[ERROR] --root 必须是目录：{root}", file=sys.stderr)
        return 2

    parser = VerilogParser(root)
    db = parser.parse_project()

    agent = VerilogDebugAgent(db, top=args.top)
    agent.analyze(trace_signal=args.trace)

    out_path = Path(args.out).expanduser().resolve()
    dot_path = Path(args.dot).expanduser().resolve()

    ReportWriter(db, agent).write_markdown(out_path)
    dot_path.write_text(agent.build_dot(), encoding="utf-8")

    risk_count = sum(1 for f in db.findings if f.level == "RISK")
    warn_count = sum(1 for f in db.findings if f.level == "WARN")
    info_count = sum(1 for f in db.findings if f.level == "INFO")

    print("[OK] 分析完成")
    print(f"[OK] Markdown 报告：{out_path}")
    print(f"[OK] 层次图 DOT：{dot_path}")
    print(
        f"[SUMMARY] modules={len(db.modules)}, "
        f"files={len(db.files)}, "
        f"RISK={risk_count}, WARN={warn_count}, INFO={info_count}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())