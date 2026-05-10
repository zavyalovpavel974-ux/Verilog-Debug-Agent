# Verilog Debug Agent

这是一个面向 FPGA 视频链路调试的 Verilog 静态分析 Agent。

## 项目简介

本项目用于辅助分析 FPGA/Verilog 工程，尤其适用于 MIPI、HDMI、Debayer、FIFO、RGB 转换、视频同步信号等复杂视频链路场景。

它可以自动扫描 Verilog/SystemVerilog 文件，提取模块层次、端口连接、时钟复位信号和视频同步信号，并生成 Markdown 分析报告和 Graphviz 层次图。

## 项目解决的核心痛点

在 FPGA 视频链路工程中，模块数量多、信号连接复杂，人工排查时钟、复位、同步信号和数据通路时效率较低，容易遗漏问题。

常见问题包括：

- reset 极性写反
- 例化端口空连接
- 一个 always 块中出现多个时钟边沿
- 时序 always 中使用阻塞赋值
- 组合 always 中使用非阻塞赋值
- vs、hs、de、valid、data 信号不同步
- frame_stable、frame_start 等帧控制信号异常
- MIPI 输入链路和 HDMI 输出链路之间数据不对齐

本项目希望将人工阅读代码的过程转化为自动化静态分析流程，提高调试效率。

## 核心功能

- 自动扫描 Verilog/SystemVerilog 工程文件
- 提取 module、port、wire、reg、assign、always 和模块例化关系
- 构建模块层次结构
- 识别 clock、reset、vs、hs、de、valid、frame、MIPI、HDMI 等关键信号
- 检查常见 Verilog 风险
- 支持追踪指定信号
- 生成 Markdown 分析报告
- 生成 Graphviz DOT 层次图

## 核心逻辑流

本项目采用类似 Agent 的长链分析流程：

1. 代码解析 Agent：扫描整个 Verilog 工程，提取模块、端口、信号、always 块和 assign 语句。
2. 结构分析 Agent：根据模块例化关系构建工程层次结构。
3. 信号识别 Agent：根据命名规则识别 clock、reset、video、MIPI、HDMI 等关键信号。
4. 风险检查 Agent：检查空端口、复位极性、多时钟 always、多驱动等常见问题。
5. 报告生成 Agent：输出 Markdown 调试报告和 DOT 层次图。

因此，本项目不是简单的代码扫描工具，而是按照“工程解析—结构分析—信号识别—风险检查—报告生成”的长链推理流程完成分析。

## 使用方法

基本运行命令：

```bash
python verilog_agent.py --root ./rtl --top top --out report.md