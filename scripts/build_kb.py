"""
一键建库脚本：

    python scripts/build_kb.py            # 幂等建库，已有数据则跳过
    python scripts/build_kb.py --rebuild  # 删掉旧集合，全量重建

前置条件：.env 里配好 embedding 相关配置（默认用智谱 embedding-3）。
"""
import os
import sys

# 让脚本能从项目根目录 import rag（脚本不在根目录，需要手动加 path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag import build_knowledge_base  # noqa: E402


def main():
    rebuild = "--rebuild" in sys.argv
    try:
        stats = build_knowledge_base(rebuild=rebuild)
        print("统计信息:", stats)
    except RuntimeError as e:
        print(f"\n[建库失败] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
