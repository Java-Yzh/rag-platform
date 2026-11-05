"""
冒烟测试：不启动服务、不调 API，单独验证入库管线的前两步（转换 + 切分）。

用法：cd backend && uv run python -m scripts.smoke_pipeline <文件路径>
     （不传文件路径则用一份内置的 Markdown 样例）

为什么要有这个脚本：管线四步里"向量化/入库"依赖智谱 API，
而"转换 + 切分"是纯本地计算——把它拆出来单独测，出问题时能快速
定位是本地处理挂了还是 API 配置挂了。
"""

import sys
from pathlib import Path

# 让脚本能找到 app 包（从 backend/ 目录运行时本来就行，这里兜底）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ingestion import _chunk_document, _convert_file  # noqa: E402

SAMPLE = """# 智能音箱产品手册

## 第一章 产品规格

本产品支持 Wi-Fi 6 与蓝牙 5.3 双模连接，内置四麦克风阵列。

| 型号 | 颜色 | 重量 |
|------|------|------|
| X1 | 白色 | 350g |
| X2 | 黑色 | 360g |

## 第二章 常见问题

### 保修政策

整机保修一年，人为损坏不在保修范围内。

### 退货政策

自签收之日起 7 天内支持无理由退货，需保持包装完整。
"""


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).expanduser().resolve()
        ext = path.suffix.lower()
    else:
        path = Path("/tmp/rag-smoke-sample.md")
        path.write_text(SAMPLE, encoding="utf-8")
        ext = ".md"

    print(f"[1/2] 转换: {path}")
    doc = _convert_file(path, ext)
    print(f"      OK, 文档名: {doc.name}")

    print("[2/2] 切分 (HybridChunker):")
    chunks = _chunk_document(doc)
    print(f"      OK, 共 {len(chunks)} 块\n")
    for c in chunks:
        head = c["content"].replace("\n", " ")[:60]
        print(f"  - #{c['seq']} [页{c['page_no']}] [{c['heading_path']}] {head}")


if __name__ == "__main__":
    main()
