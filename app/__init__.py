"""CleanPlate —— 本地 AI 去杂物工具。

基于 LaMa（Large Mask Inpainting）模型，全程本地推理，照片不出本机。
推理实现参考了开源项目 IOPaint（原 LaMa Cleaner，Sanster/IOPaint，Apache-2.0）的
LaMa 模型封装方式，但去掉了其庞大的 Web/Gradio 依赖，只保留核心的
「模型加载 + 前向推理 + 补边/归一化」逻辑。
"""

__version__ = "0.02"
__app_name__ = "CleanPlate"
