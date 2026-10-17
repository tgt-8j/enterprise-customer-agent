"""RAG 模块单元测试。

测试策略：
- chunk_text：纯函数测试，不需要外部依赖
- embed_texts：mock httpx.post，验证请求参数
- search：mock embed_texts 和 collection，验证检索逻辑

不涉及真实的 embedding API 调用或 ChromaDB 写入。
"""

from unittest.mock import MagicMock, patch

import pytest


class TestChunkText:
    """文本切分逻辑测试。"""

    def test_short_text_single_chunk(self):
        """短文本不切分。"""
        from src.retrieval.rag import chunk_text

        chunks = chunk_text("这是一段短文本", chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == "这是一段短文本"

    def test_long_text_splits(self):
        """长文本按 chunk_size 切分。"""
        from src.retrieval.rag import chunk_text

        text = "段落一。\n\n段落二。\n\n段落三。" * 10
        chunks = chunk_text(text, chunk_size=20, overlap=5)
        assert len(chunks) > 1
        # 每个 chunk 不应超过 chunk_size + overlap
        for c in chunks:
            assert len(c) <= 25

    def test_paragraph_aware_splitting(self):
        """按段落边界切分，不破坏语义。"""
        from src.retrieval.rag import chunk_text

        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        chunks = chunk_text(text, chunk_size=50)
        # 应该按段落合并，而不是按字符硬切
        assert all("第一段" in c or "第二段" in c or "第三段" in c for c in chunks)

    def test_overlap_conservation(self):
        """重叠部分正确保留。"""
        from src.retrieval.rag import chunk_text

        text = "A B C D E F G H I J"
        chunks = chunk_text(text, chunk_size=10, overlap=3)
        if len(chunks) > 1:
            # 相邻 chunk 应该有重叠
            last_of_first = chunks[0][-3:]
            first_of_second = chunks[1][:3]
            assert last_of_first == first_of_second


class TestEmbedTexts:
    """向量化函数测试（mock HTTP）。"""

    @pytest.mark.asyncio
    async def test_embed_texts_mocked(self):
        """mock httpx 验证请求参数正确。"""
        from src.retrieval.rag import embed_texts

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * 10}]}

        with (
            patch("src.retrieval.rag.EMBEDDING_BASE_URL", "http://test"),
            patch("src.retrieval.rag.EMBEDDING_API_KEY", "test_key"),
            patch("src.retrieval.rag.EMBEDDING_MODEL_NAME", "embedding-3"),
        ):
            with patch("httpx.post", return_value=mock_response) as mock_post:
                vectors = embed_texts(["test"])
                assert len(vectors) == 1
                assert len(vectors[0]) == 10
                # 验证请求参数
                call_args = mock_post.call_args
                assert call_args.kwargs["json"]["model"] == "embedding-3"
                assert call_args.kwargs["json"]["dimensions"] == 1024

    def test_embed_texts_missing_config(self):
        """缺少配置时抛出 RuntimeError。"""
        from src.retrieval.rag import embed_texts

        with (
            patch("src.retrieval.rag.EMBEDDING_BASE_URL", ""),
            patch("src.retrieval.rag.EMBEDDING_API_KEY", ""),
        ):
            with pytest.raises(RuntimeError, match="缺少 embedding 配置"):
                embed_texts(["test"])


class TestSearch:
    """检索逻辑测试。"""

    def test_search_empty_collection(self):
        """空知识库返回建库提示。"""
        from src.retrieval.rag import search

        with patch("src.retrieval.rag.get_collection") as mock_get:
            mock_coll = MagicMock()
            mock_coll.count.return_value = 0
            mock_get.return_value = mock_coll

            result = search("test query")
            assert "尚未建立" in result

    def test_search_no_hits_above_threshold(self):
        """所有结果相似度低于阈值时返回未找到。"""
        from src.retrieval.rag import search

        with patch("src.retrieval.rag.get_collection") as mock_get:
            mock_coll = MagicMock()
            mock_coll.count.return_value = 1
            mock_get.return_value = mock_coll

            # cosine distance 1.0 → similarity = 1 - 1.0 = 0.0，低于 0.25 阈值
            mock_coll.query.return_value = {
                "documents": [["不相关的文本"]],
                "metadatas": [[{"source": "test"}]],
                "distances": [[1.0]],  # similarity = 1-1.0 = 0.0 < 0.25
            }

            with patch("src.retrieval.rag.embed_texts", return_value=[[0.1] * 10]):
                result = search("test query")
                assert "未找到" in result

    def test_search_with_hits(self):
        """有匹配结果时返回格式化文本。"""
        from src.retrieval.rag import search

        with patch("src.retrieval.rag.get_collection") as mock_get:
            mock_coll = MagicMock()
            mock_coll.count.return_value = 1
            mock_get.return_value = mock_coll

            mock_coll.query.return_value = {
                "documents": [["退货政策内容"]],
                "metadatas": [[{"source": "01-退货政策总则"}]],
                "distances": [[0.1]],  # similarity = 0.9 > 0.25
            }

            with patch("src.retrieval.rag.embed_texts", return_value=[[0.1] * 10]):
                result = search("退货政策")
                assert "退货政策" in result
                assert "01-退货政策总则" in result

    def test_search_exception_fallback(self):
        """检索异常时返回降级文本。"""
        from src.retrieval.rag import search

        with patch("src.retrieval.rag.get_collection", side_effect=Exception("test error")):
            result = search("test")
            assert "检索失败" in result


class TestHybridFallback:
    """Hybrid 检索的两条关键保证。

    1. BM25 索引必须能在服务进程里懒加载（此前只在建库脚本里初始化，
       线上服务进程永远是 None，Hybrid 名存实亡）；
    2. embedding 故障时 BM25 结果必须留下来（此前被 reranker 的
       `similarity > 0.3` 全部滤掉，返回「未找到相关信息」）。
    """

    def test_bm25_is_lazily_built_from_collection(self):
        """未建过索引时，从 Chroma 回灌文档重建，检索能命中。"""
        from src.retrieval.rag import search

        mock_coll = MagicMock()
        mock_coll.count.return_value = 1
        mock_coll.get.return_value = {
            "documents": ["支持七天无理由退货。"],
            "metadatas": [{"source": "02-七天无理由退货"}],
        }
        mock_coll.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}

        with (
            patch("src.retrieval.rag.get_collection", return_value=mock_coll),
            patch("src.retrieval.rag.embed_texts", return_value=[[0.1] * 4]),
        ):
            result = search("退货")

        assert "未找到" not in result
        assert "02-七天无理由退货" in result
        assert "仅 BM25 命中" in result

    def test_embedding_failure_degrades_to_bm25(self):
        """embedding 挂掉时仍返回 BM25 命中，而不是「未找到」。"""
        from src.retrieval.bm25 import BM25Retriever
        from src.retrieval.rag import search

        retriever = BM25Retriever()
        retriever.build(["支持七天无理由退货。"], ["02-七天无理由退货"])

        mock_coll = MagicMock()
        mock_coll.count.return_value = 1

        with (
            patch("src.retrieval.rag.get_collection", return_value=mock_coll),
            patch("src.retrieval.rag._bm25_retriever", retriever),
            patch("src.retrieval.rag.embed_texts", side_effect=RuntimeError("embedding down")),
        ):
            result = search("退货")

        assert "未找到" not in result
        assert "02-七天无理由退货" in result

    def test_reranker_keeps_bm25_only_candidates(self):
        """第一级粗筛只丢「有向量分但偏低」的，不能丢「没有向量分」的。"""
        from src.retrieval.reranker import two_stage_rerank

        results = two_stage_rerank(
            [
                {"document": "只被 BM25 召回", "source": "01-退货"},
                {"document": "向量分很低", "source": "02-物流"},
                {"document": "向量分很高", "source": "03-价保"},
            ],
            [None, 0.1, 0.9],
        )

        documents = [r.document for r in results]
        assert "只被 BM25 召回" in documents
        assert "向量分很低" not in documents
        assert "向量分很高" in documents
        assert next(r for r in results if r.document == "只被 BM25 召回").similarity is None

    def test_search_honours_top_k(self):
        """top_k 之前是接了不用的摆设参数，最终条数写死成常量。"""
        from src.retrieval.bm25 import BM25Retriever
        from src.retrieval.rag import search

        retriever = BM25Retriever()
        retriever.build(
            [
                "退货政策一：七天无理由。",
                "退货政策二：商品需完好。",
                "退货政策三：运费由商家承担。",
            ],
            ["01-退货总则", "02-七天无理由", "04-退货运费"],
        )

        mock_coll = MagicMock()
        mock_coll.count.return_value = 3

        with (
            patch("src.retrieval.rag.get_collection", return_value=mock_coll),
            patch("src.retrieval.rag._bm25_retriever", retriever),
            patch("src.retrieval.rag.embed_texts", side_effect=RuntimeError("down")),
        ):
            single = search("退货", top_k=1)
            default = search("退货")

        assert "[1]" in single
        assert "[2]" not in single
        assert "[3]" in default  # 默认仍然取 Top-5（这里只有 3 条候选）
