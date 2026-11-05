"""
app 包入口。它必须是全项目最先被加载的模块，这里只做一件事：

设置 HuggingFace 下载源（默认官网）。

背景：Docling 转 PDF 时要从 HuggingFace 下载布局识别模型（首次约几百 MB，
之后走本地缓存，日常运行不再联网下载）。这个环境变量必须在
docling/transformers 被 import 之前设置好才生效——Python 的机制是：
谁先 `import app.xxx`，本文件就先执行。

如果不开 VPN 直连不了 huggingface.co，把系统环境变量 HF_ENDPOINT 设为
https://hf-mirror.com（社区镜像）即可覆盖这里的默认值
（setdefault 不强抢已有配置）。
"""

import os

os.environ.setdefault("HF_ENDPOINT", "https://huggingface.co")
