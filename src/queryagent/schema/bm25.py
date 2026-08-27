"""BM25 关键词检索（稀疏检索）。

对表文档做离线索引：英文按词切分，中文按字符 bigram 切分（无需词典），
用于「三路检索」中的关键词召回，与 embedding（稠密）互补。
"""
from __future__ import annotations

import math
import re
from collections import Counter

_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """英文按词、中文按字符 bigram 分词。"""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    cjk = "".join(_CJK.findall(text))
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i : i + 2])
    return tokens


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = [tokenize(d) for d in docs]
        self.k1 = k1
        self.b = b
        self.n = len(self.docs)
        self.avgdl = (sum(len(d) for d in self.docs) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for d in self.docs:
            for t in set(d):
                self.df[t] += 1
        self.idf = {
            t: math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)
            for t, df in self.df.items()
        }
        self.tf = [Counter(d) for d in self.docs]

    def scores(self, query: str) -> list[float]:
        qt = tokenize(query)
        out: list[float] = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            s = 0.0
            for t in qt:
                idf = self.idf.get(t)
                if idf is None:
                    continue
                f = tf[t]
                s += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                )
            out.append(s)
        return out
