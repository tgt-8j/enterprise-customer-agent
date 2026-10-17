"""BM25 检索单元测试。

重点覆盖两个此前不成立的行为：
1. 中文查询能真的召回（旧分词把整句当一个 token，BM25 恒为空）；
2. 检索结果带真实来源，而不是字面量 "bm25"。
"""

from src.retrieval.bm25 import BM25Retriever


def _retriever() -> BM25Retriever:
    retriever = BM25Retriever()
    retriever.build(
        [
            "支持七天无理由退货，商品需保持完好，不影响二次销售。",
            "物流异常：包裹在转运中心滞留超过 48 小时需人工介入。",
            "价保规则：下单后商品降价，可申请补差价。",
        ],
        ["02-七天无理由退货", "07-物流异常处理", "13-价保规则"],
    )
    return retriever


class TestChineseTokenization:
    def test_chinese_terms_are_retrievable(self):
        """ "退货" 这类词必须能命中含它的长句（旧实现恒为空）。"""
        retriever = _retriever()
        results = retriever.search("退货", top_k=3)
        assert results, "中文查询未召回任何文档，分词仍然有问题"
        assert results[0][1] == "02-七天无理由退货"

    def test_bigram_improves_precision(self):
        """二元组让"价保"这类专有词排到前面。"""
        retriever = _retriever()
        results = retriever.search("价保", top_k=3)
        assert results[0][1] == "13-价保规则"

    def test_ascii_terms_still_work(self):
        retriever = _retriever()
        results = retriever.search("48", top_k=3)
        assert results
        assert results[0][1] == "07-物流异常处理"


class TestRetrieverContract:
    def test_results_carry_source(self):
        retriever = _retriever()
        for _doc, source, score in retriever.search("物流", top_k=2):
            assert source != "bm25"
            assert score > 0

    def test_empty_query_returns_nothing(self):
        retriever = _retriever()
        assert retriever.search("   ", top_k=3) == []

    def test_search_before_build_returns_empty(self):
        assert BM25Retriever().search("退货") == []

    def test_build_is_idempotent(self):
        retriever = _retriever()
        before = retriever.search("退货", top_k=3)
        retriever.build(["完全不同的语料"], ["x"])
        assert retriever.search("退货", top_k=3) == before

    def test_build_without_sources_uses_placeholder(self):
        retriever = BM25Retriever()
        retriever.build(["退货政策"])
        assert retriever.search("退货")[0][1] == "未知来源"
