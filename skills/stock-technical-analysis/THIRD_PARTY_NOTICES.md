# 第三方来源与适配说明

本Skill新增代码采用仓库MIT许可。谐波检测组织、比例核对及D价格代数参考djoffrey/HarmonicPatterns，固定提交 `1e0cd81a96132a1c3337dbbfce39f13d4606758b`，包声明0.0.1。

来源：https://github.com/djoffrey/HarmonicPatterns/tree/1e0cd81a96132a1c3337dbbfce39f13d4606758b

本项目独立重写有限标准库核心，采用名为project-harmonic-v1的显式变体；改用对称相对容差、连续区间、逐项完整校验、PRZ交集、有身份的状态与事件。Gartley/Bat范围及预测分支不照搬上游默认，差异见专题说明书。原get_zigzag（含未核完整授权的TradingView来源）、全量扫描去重、网络取数、绘图、示例与依赖均未纳入本包；没有通过改名翻译沿用这些代码。

原作者网页仅用于比例锚点交叉核对，未复制其图文、商标授权表述或准确率宣传。本包不包含Longbridge、MyTT、hikyuu、QUANTAXIS、KLineCharts等项目代码；分层组织及机制检查参考方法，不整平台引入。

NumPy、TA-Lib、Matplotlib与Plotly为独立运行依赖，不随包分发，按各自许可安装。固定环境采用TA-Lib Python 0.6.8（项目BSD-2-Clause，所带C库按其原许可），NumPy2.2.6、Matplotlib3.9.2、Plotly5.24.1；requirements只记录依赖，不将它们改授本仓库MIT。SMC有限规则、一目及海龟条件为本项目明示实现，不宣称复制或完整覆盖任何外部平台。

以下保留上述HarmonicPatterns来源的MIT版权／许可：

```text
MIT License

Copyright (c) 2021 Joffrey

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

