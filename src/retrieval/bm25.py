"""BM25 关键词检索模块。

与向量检索互补：关键词匹配擅长精确术语匹配（订单号、策略名），
向量检索擅长语义理解（"我的快递到哪了"）。两者通过 RRF 融合后
再由 reranker 精排，构成三级检索链路。
"""

import math
import re
import threading
from collections import Counter

# BM25 参数（罗伯逊-斯维科夫公式经验值）
K1 = 1.5   # 词频饱和参数，越大越容忍高频词
B = 0.75   # 文档长度归一化参数，越大越偏向长文档


class BM25Index:
    """轻量级 BM25 索引，适合中小规模知识库（<10万文档）。

    字段说明：
    - doc_lengths: 每篇文档的词数
    - term_freqs: 每篇文档中每个词的出现次数
    - df: 包含该词的文档数（document frequency）
    - avgdl: 平均文档长度
    """

    def __init__(self, corpus: list[str], language: str = "zh"):
        self.corpus = corpus
        self.tokenized = [self._tokenize(doc, language) for doc in corpus]
        self.doc_lengths = [len(tokens) for tokens in self.tokenized]
        self.avgdl = sum(self.doc_lengths) / max(len(self.doc_lengths), 1)
        self._build_indexes()

    def _tokenize(self, text: str, language: str) -> list[str]:
        """简单分词：中文按字符分割（生产环境可换 jieba），英文按空格分割。"""
        if language == "zh":
            # 中文：提取所有汉字、数字、字母作为 token
            return re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]+", text.lower())
        return text.lower().split()

    def _build_indexes(self):
        """构建词频和文档频率索引。"""
        self.term_freqs: list[Counter] = []
        self.df: dict[str, int] = {}
        for tokens in self.tokenized:
            counter = Counter(tokens)
            self.term_freqs.append(counter)
            for term in counter:
                self.df[term] = self.df.get(term, 0) + 1
        self.N = len(self.corpus)

    def score(self, query_tokens: list[str], doc_index: int) -> float:
        """计算 query 对指定文档的 BM25 分数。"""
        score = 0.0
        doc_len = self.doc_lengths[doc_index]
        tf = self.term_freqs[doc_index]
        for token in query_tokens:
            df = self.df.get(token, 0)
            if df == 0:
                continue
            idf = math.log(
                (self.N - df + 0.5) / (df + 0.5) + 1.0
            )  # BM25 IDF 公式
            term_tf = tf.get(token, 0)
            numerator = term_tf * (K1 + 1)
            denominator = term_tf + K1 * (
                1 - B + B * doc_len / self.avgdl
            )
            score += idf * numerator / denominator
        return score

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """对 query 检索 Top-K 文档，返回 (doc_index, score) 列表。"""
        tokens = self._tokenize(query, "zh")
        scores = []
        for i in range(len(self.corpus)):
            s = self.score(tokens, i)
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


class BM25Retriever:
    """BM25 检索器，封装索引生命周期。"""

    def __init__(self, language: str = "zh"):
        self.language = language
        self.index: BM25Index | None = None
        self._lock = threading.Lock()
        self._built = False

    def build(self, documents: list[str]) -> None:
        """用文档列表构建 BM25 索引。幂等：已构建则跳过。"""
        with self._lock:
            if self._built and self.index is not None:
                return
            self.index = BM25Index(documents, self.language)
            self._built = True

    def search(
        self, query: str, top_k: int = 10
    ) -> list[tuple[str, float]]:
        """检索并返回 [(document_text, score), ...]。未建索引时返回空列表。"""
        if self.index is None:
            return []
        results = self.index.search(query, top_k)
        return [
            (self.corpus[idx], score) for idx, score in results
        ]

    @property
    def corpus(self) -> list[str]:
        if self.index is None:
            return []
        return self.index.corpus
