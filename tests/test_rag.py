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
        from rag import chunk_text
        chunks = chunk_text("这是一段短文本", chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == "这是一段短文本"

    def test_long_text_splits(self):
        """长文本按 chunk_size 切分。"""
        from rag import chunk_text
        text = "段落一。\n\n段落二。\n\n段落三。" * 10
        chunks = chunk_text(text, chunk_size=20, overlap=5)
        assert len(chunks) > 1
        # 每个 chunk 不应超过 chunk_size + overlap
        for c in chunks:
            assert len(c) <= 25

    def test_paragraph_aware_splitting(self):
        """按段落边界切分，不破坏语义。"""
        from rag import chunk_text
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        chunks = chunk_text(text, chunk_size=50)
        # 应该按段落合并，而不是按字符硬切
        assert all("第一段" in c or "第二段" in c or "第三段" in c for c in chunks)

    def test_overlap_conservation(self):
        """重叠部分正确保留。"""
        from rag import chunk_text
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
        from rag import embed_texts

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"index": 0, "embedding": [0.1] * 10}]
        }

        with patch("rag.EMBEDDING_BASE_URL", "http://test"):
            with patch("rag.EMBEDDING_API_KEY", "test_key"):
                with patch("httpx.post", return_value=mock_response) as mock_post:
                    vectors = embed_texts(["test"])
                    assert len(vectors) == 1
                    assert len(vectors[0]) == 10
                    # 验证请求参数
                    call_args = mock_post.call_args
                    assert call_args.kwargs["json"]["model"] == "text-embedding-v3"
                    assert call_args.kwargs["json"]["dimensions"] == 1024

    def test_embed_texts_missing_config(self):
        """缺少配置时抛出 RuntimeError。"""
        from rag import embed_texts

        with patch("rag.EMBEDDING_BASE_URL", ""), patch("rag.EMBEDDING_API_KEY", ""):
            with pytest.raises(RuntimeError, match="缺少 embedding 配置"):
                embed_texts(["test"])


class TestSearch:
    """检索逻辑测试。"""

    def test_search_empty_collection(self):
        """空知识库返回建库提示。"""
        from rag import search

        with patch("rag.get_collection") as mock_get:
            mock_coll = MagicMock()
            mock_coll.count.return_value = 0
            mock_get.return_value = mock_coll

            result = search("test query")
            assert "尚未建立" in result

    def test_search_no_hits_above_threshold(self):
        """所有结果相似度低于阈值时返回未找到。"""
        from rag import search

        with patch("rag.get_collection") as mock_get:
            mock_coll = MagicMock()
            mock_coll.count.return_value = 1
            mock_get.return_value = mock_coll

            # cosine distance 1.0 → similarity = 1 - 1.0 = 0.0，低于 0.25 阈值
            mock_coll.query.return_value = {
                "documents": [["不相关的文本"]],
                "metadatas": [[{"source": "test"}]],
                "distances": [[1.0]],  # similarity = 1-1.0 = 0.0 < 0.25
            }

            with patch("rag.embed_texts", return_value=[[0.1] * 10]):
                result = search("test query")
                assert "未找到" in result

    def test_search_with_hits(self):
        """有匹配结果时返回格式化文本。"""
        from rag import search

        with patch("rag.get_collection") as mock_get:
            mock_coll = MagicMock()
            mock_coll.count.return_value = 1
            mock_get.return_value = mock_coll

            mock_coll.query.return_value = {
                "documents": [["退货政策内容"]],
                "metadatas": [[{"source": "01-退货政策总则"}]],
                "distances": [[0.1]],  # similarity = 0.9 > 0.25
            }

            with patch("rag.embed_texts", return_value=[[0.1] * 10]):
                result = search("退货政策")
                assert "退货政策" in result
                assert "01-退货政策总则" in result

    def test_search_exception_fallback(self):
        """检索异常时返回降级文本。"""
        from rag import search

        with patch("rag.get_collection", side_effect=Exception("test error")):
            result = search("test")
            assert "检索失败" in result
