"""Knowledge retrieval: pure-Python BM25 (Unicode/Bangla aware) with optional embedding hybrid."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import KnowledgeChunk, KnowledgeDoc

log = logging.getLogger("omniqms.retrieval")

# \w misses Indic combining vowel signs, so include the Indic blocks + ZWJ/ZWNJ explicitly.
_TOKEN_RE = re.compile("[\\w\u0900-\u0963\u0966-\u0DFF\u200c\u200d]+", re.UNICODE)  # excludes danda
_STOP = set("""a an and are as at be but by can do does for from has have how i if in is it its me my
of on or our please so that the their them there this to us was we what when where which who why
will with you your how many much did any some hi hello dear sir madam ki ache kemon ami amar apni apnar ar ba je koto kto lage hoy hobe kivabe kothay kobe keno""".split())
_STOP |= {"আমি", "আমার", "আপনি", "আপনার", "কি", "কী", "এর", "ও", "এবং", "যে", "এই", "আছে", "করে", "জন্য", "না", "হয়", "হবে", "কত", "কতো", "কোথায়", "কখন", "কেন", "কিভাবে", "কীভাবে"}

MAX_CHUNKS_SCANNED = 5000


def _stem(tok: str) -> str:
    if tok.isascii():
        if len(tok) > 4 and tok.endswith("ies"):
            return tok[:-3] + "y"
        if len(tok) > 5 and tok.endswith("ing"):
            return tok[:-3]
        if len(tok) > 4 and tok.endswith("ed"):
            return tok[:-2]
        if len(tok) > 4 and tok.endswith("es") and tok[-3] in "sxz" or tok.endswith(("ches", "shes")):
            return tok[:-2]
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            return tok[:-1]
        return tok
    # crude Bangla suffix stripping (case markers / plural)
    for suf in ("গুলো", "গুলি", "দের", "েরা", "ের", "টা", "টি", "রা", "কে", "তে", "য়"):
        if len(tok) > len(suf) + 1 and tok.endswith(suf):
            return tok[: -len(suf)]
    # possessive 'র' after a vowel sign (ঢাকার → ঢাকা), locative 'ে' (ভিতরে → ভিতর)
    if len(tok) > 3 and tok.endswith("র") and "া" <= tok[-2] <= "ৌ":
        return tok[:-1]
    if len(tok) > 3 and tok.endswith("ে"):
        return tok[:-1]
    return tok


def tokenize(text: str) -> list[str]:
    out = []
    for t in _TOKEN_RE.findall((text or "").casefold()):
        t = t.strip("_‌‍")
        if not t or t in _STOP or (t.isascii() and len(t) < 2):
            continue
        out.append(_stem(t))
    return out


@dataclass
class ScoredChunk:
    chunk_id: int
    doc_id: int
    title: str
    text: str
    score: float  # combined relevance (unnormalised)
    coverage: float  # 0..1 share of query idf mass found in this chunk


@dataclass
class RetrievalResult:
    chunks: list[ScoredChunk]
    signal: float  # 0..1 overall retrieval confidence


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    dbb = math.sqrt(sum(y * y for y in b))
    return num / (da * dbb) if da and dbb else 0.0


def retrieve(db: Session, workspace_id: int, query: str, top_k: int = 4) -> RetrievalResult:
    rows = db.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.doc_id, KnowledgeChunk.text, KnowledgeDoc.title)
        .join(KnowledgeDoc, KnowledgeDoc.id == KnowledgeChunk.doc_id)
        .where(KnowledgeChunk.workspace_id == workspace_id, KnowledgeDoc.workspace_id == workspace_id)
        .limit(MAX_CHUNKS_SCANNED)
    ).all()
    q_terms = tokenize(query)
    if not rows or not q_terms:
        return RetrievalResult([], 0.0)

    docs = [tokenize(r.text) for r in rows]
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    q_unique = list(dict.fromkeys(q_terms))

    def idf(t: str) -> float:
        return math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))

    q_idf = {t: idf(t) for t in q_unique}
    seen = [v for t, v in q_idf.items() if df.get(t)]
    if not seen:
        return RetrievalResult([], 0.0)
    # Terms never seen in the KB weigh as much as an average known term (not the max idf),
    # so one unusual word does not sink an otherwise well-covered question.
    avg_seen = sum(seen) / len(seen)
    total_idf = sum(v if df.get(t) else avg_seen for t, v in q_idf.items()) or 1.0
    k1, b = 1.5, 0.75
    scored: list[ScoredChunk] = []
    for r, d in zip(rows, docs):
        tf = Counter(d)
        dl = len(d) or 1
        s = 0.0
        covered = 0.0
        for t in q_unique:
            f = tf.get(t, 0)
            if not f:
                continue
            covered += q_idf[t]
            s += q_idf[t] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scored.append(ScoredChunk(r.id, r.doc_id, r.title, r.text, s, covered / total_idf))

    s = get_settings()
    if s.embeddings == "openai" and s.openai_api_key:
        try:
            scored = _hybrid(db, rows, query, scored)
        except Exception as e:  # never let embeddings break answering
            log.warning("embedding hybrid failed: %s", type(e).__name__)

    scored.sort(key=lambda c: (c.score, c.coverage), reverse=True)
    top = scored[:top_k]
    signal = top[0].coverage if top else 0.0
    return RetrievalResult(top, round(min(max(signal, 0.0), 1.0), 4))


def _embed(texts: list[str]) -> list[list[float]]:
    import httpx
    s = get_settings()
    r = httpx.post("https://api.openai.com/v1/embeddings", timeout=20,
                   headers={"Authorization": f"Bearer {s.openai_api_key}"},
                   json={"model": s.openai_embedding_model, "input": texts})
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]


def _hybrid(db: Session, rows, query: str, scored: list[ScoredChunk]) -> list[ScoredChunk]:
    by_id = {c.chunk_id: c for c in scored}
    chunks = {c.id: c for c in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.id.in_([r.id for r in rows])))}
    need = [c for c in chunks.values() if not c.embedding]
    for i in range(0, len(need), 64):
        batch = need[i:i + 64]
        for c, e in zip(batch, _embed([c.text for c in batch])):
            c.embedding = e
    db.commit()
    qv = _embed([query])[0]
    max_bm25 = max((c.score for c in scored), default=1.0) or 1.0
    out = []
    for r in rows:
        c = chunks[r.id]
        cos = _cosine(qv, c.embedding or [])
        sem = min(max((cos - 0.2) / 0.6, 0.0), 1.0)
        base = by_id.get(r.id)
        bm = (base.score / max_bm25) if base else 0.0
        cov = max(base.coverage if base else 0.0, sem)
        comb = 0.5 * bm + 0.5 * sem
        if comb > 0.05:
            out.append(ScoredChunk(r.id, r.doc_id, r.title, r.text, comb, cov))
    return out
