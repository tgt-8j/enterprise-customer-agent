from unittest.mock import MagicMock, patch

import pytest


class TestCircuitBreaker:
    """熔断器逻辑测试。"""

    def test_check_circuit_closed(self):
        from rag import _check_circuit, _circuit, _CircuitState

        original = dict(_circuit)
        try:
            _circuit["state"] = _CircuitState.CLOSED
            _check_circuit()
        finally:
            _circuit.clear()
            _circuit.update(original)

    def test_check_circuit_open_not_elapsed(self):
        import time

        from rag import _check_circuit, _circuit, _CircuitState

        original = dict(_circuit)
        try:
            _circuit["state"] = _CircuitState.OPEN
            _circuit["last_failure_time"] = time.time()
            with pytest.raises(RuntimeError, match="OPEN"):
                _check_circuit()
        finally:
            _circuit.clear()
            _circuit.update(original)

    def test_check_circuit_open_elapsed_becomes_half_open(self):
        import time

        from rag import _check_circuit, _circuit, _CircuitState

        original = dict(_circuit)
        try:
            _circuit["state"] = _CircuitState.OPEN
            _circuit["last_failure_time"] = time.time() - 9999
            _check_circuit()
            assert _circuit["state"] == _CircuitState.HALF_OPEN
        finally:
            _circuit.clear()
            _circuit.update(original)

    def test_record_circuit_failure_triggers_open(self):
        import rag
        from rag import _circuit, _CircuitState, _record_circuit_failure

        original = dict(_circuit)
        orig_threshold = rag.EMBEDDING_FAILURE_THRESHOLD
        try:
            rag.EMBEDDING_FAILURE_THRESHOLD = 2
            _circuit["consecutive_failures"] = 1
            _record_circuit_failure()
            assert _circuit["state"] == _CircuitState.OPEN
        finally:
            rag.EMBEDDING_FAILURE_THRESHOLD = orig_threshold
            _circuit.clear()
            _circuit.update(original)

    def test_record_circuit_success_resets(self):
        from rag import _circuit, _CircuitState, _record_circuit_success

        original = dict(_circuit)
        try:
            _circuit["state"] = _CircuitState.OPEN
            _circuit["consecutive_failures"] = 5
            _record_circuit_success()
            assert _circuit["state"] == _CircuitState.CLOSED
            assert _circuit["consecutive_failures"] == 0
        finally:
            _circuit.clear()
            _circuit.update(original)


class TestLoadDocuments:
    """文档加载测试。"""

    def test_load_documents_finds_md_files(self, tmp_path):
        from rag import load_documents

        doc = tmp_path / "test_doc.md"
        doc.write_text("# Title\nContent here", encoding="utf-8")
        with patch("rag.KNOWLEDGE_DIR", str(tmp_path)):
            docs = load_documents()
            assert len(docs) == 1
            assert docs[0]["source"] == "test_doc"
            assert "Content here" in docs[0]["text"]

    def test_load_documents_ignores_non_md(self, tmp_path):
        from rag import load_documents

        doc = tmp_path / "ignore.jpg"
        doc.write_text("image data")
        with patch("rag.KNOWLEDGE_DIR", str(tmp_path)):
            docs = load_documents()
            assert len(docs) == 0

    def test_load_documents_skips_empty(self, tmp_path):
        from rag import load_documents

        doc = tmp_path / "empty.md"
        doc.write_text("")
        with patch("rag.KNOWLEDGE_DIR", str(tmp_path)):
            docs = load_documents()
            assert len(docs) == 0


class TestChunkTextExtended:
    """chunk_text 边界情况测试。"""

    def test_markdown_header_handling(self):
        from rag import chunk_text

        text = "# Chapter1\nParagraph 1.\n\n## Chapter 2\nParagraph 2."
        chunks = chunk_text(text, chunk_size=100)
        # 标题行被 strip 掉 # 前缀后保留在 chunk 中
        flat = " ".join(chunks)
        assert "Chapter1" in flat
        assert "Chapter 2" in flat

    def test_very_long_paragraph_sliding_window(self):
        from rag import chunk_text

        long_para = "A" * 500
        chunks = chunk_text(long_para, chunk_size=100, overlap=20)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 100
        if len(chunks) > 1:
            assert chunks[0][-20:] == chunks[1][:20]


class TestEmbedTextsExtended:
    """embed_texts 异常分支测试。"""

    @pytest.mark.asyncio
    async def test_embed_texts_timeout(self):
        import httpx

        from rag import _circuit, embed_texts

        original = dict(_circuit)
        try:
            with (
                patch("rag.EMBEDDING_BASE_URL", "http://test"),
                patch("rag.EMBEDDING_API_KEY", "key"),
                patch("httpx.post", side_effect=httpx.TimeoutException("timeout")),
            ):
                with pytest.raises(RuntimeError, match="超时"):
                    await embed_texts(["test"])
                assert _circuit["consecutive_failures"] >= 1
        finally:
            _circuit.clear()
            _circuit.update(original)

    @pytest.mark.asyncio
    async def test_embed_texts_request_error(self):
        import httpx

        from rag import _circuit, embed_texts

        original = dict(_circuit)
        try:
            with (
                patch("rag.EMBEDDING_BASE_URL", "http://test"),
                patch("rag.EMBEDDING_API_KEY", "key"),
                patch("httpx.post", side_effect=httpx.RequestError("refused")),
            ):
                with pytest.raises(RuntimeError, match="请求失败"):
                    await embed_texts(["test"])
                assert _circuit["consecutive_failures"] >= 1
        finally:
            _circuit.clear()
            _circuit.update(original)

    @pytest.mark.asyncio
    async def test_embed_texts_non_200(self):
        from rag import _circuit, embed_texts

        original = dict(_circuit)
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            with (
                patch("rag.EMBEDDING_BASE_URL", "http://test"),
                patch("rag.EMBEDDING_API_KEY", "key"),
                patch("httpx.post", return_value=mock_resp),
            ):
                with pytest.raises(RuntimeError, match="500"):
                    await embed_texts(["test"])
                assert _circuit["consecutive_failures"] >= 1
        finally:
            _circuit.clear()
            _circuit.update(original)


class TestBuildKnowledgeBase:
    """建库逻辑测试。"""

    def test_build_skips_when_exists(self):
        from rag import build_knowledge_base

        mock_coll = MagicMock()
        mock_coll.count.return_value = 100
        with (
            patch("rag.load_documents", return_value=[{"source": "test", "text": "content"}]),
            patch("rag.get_collection", return_value=mock_coll),
        ):
            result = build_knowledge_base()
            assert result["skipped"] is True
            mock_coll.add.assert_not_called()

    def test_build_rebuild_forces_reload(self):
        from rag import build_knowledge_base

        mock_coll = MagicMock()
        mock_coll.count.return_value = 100
        mock_client = MagicMock()
        mock_coll._client = mock_client
        with (
            patch("rag.load_documents", return_value=[{"source": "test", "text": "content"}]),
            patch("rag.get_collection", return_value=mock_coll),
            patch("rag.embed_texts", return_value=[[0.1] * 10]),
        ):
            result = build_knowledge_base(rebuild=True)
            mock_client.delete_collection.assert_called_once()
            mock_coll.add.assert_called_once()
            assert result["skipped"] is False

    def test_build_no_docs_raises(self):
        from rag import build_knowledge_base

        with patch("rag.load_documents", return_value=[]):
            with pytest.raises(RuntimeError, match="没有找到任何"):
                build_knowledge_base()


class TestSearchExtended:
    """search 函数边界测试。"""

    def test_search_empty_documents_list(self):
        from rag import search

        mock_coll = MagicMock()
        mock_coll.count.return_value = 1
        mock_coll.query.return_value = {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        with (
            patch("rag.get_collection", return_value=mock_coll),
            patch("rag.embed_texts", return_value=[[0.1] * 10]),
        ):
            result = search("test")
            assert "未找到" in result

    def test_search_all_below_threshold(self):
        from rag import search

        mock_coll = MagicMock()
        mock_coll.count.return_value = 1
        mock_coll.query.return_value = {
            "documents": [["irrelevant"]],
            "metadatas": [[{"source": "test"}]],
            "distances": [[2.0]],
        }
        with (
            patch("rag.get_collection", return_value=mock_coll),
            patch("rag.embed_texts", return_value=[[0.1] * 10]),
        ):
            result = search("test")
            assert "未找到" in result
