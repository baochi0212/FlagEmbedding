import os
import faiss
import numpy as np
import pytrec_eval
from tqdm import tqdm
from collections import defaultdict
from typing import Dict, List, Tuple


# Modified from https://github.com/beir-cellar/beir/blob/f062f038c4bfd19a8ca942a9910b1e0d218759d4/beir/retrieval/custom_metrics.py#L4
def evaluate_mrr(
    qrels: Dict[str, Dict[str, int]],
    results: Dict[str, Dict[str, float]],
    k_values: List[int],
) -> Tuple[Dict[str, float]]:
    mrr = defaultdict(list)

    k_max, top_hits = max(k_values), {}

    for query_id, doc_scores in results.items():
        top_hits[query_id] = sorted(
            doc_scores.items(), key=lambda item: item[1], reverse=True
        )[0:k_max]

    for query_id in top_hits:
        query_relevant_docs = {
            doc_id for doc_id in qrels[query_id] if qrels[query_id][doc_id] > 0
        }
        for k in k_values:
            rr = 0
            for rank, hit in enumerate(top_hits[query_id][0:k], 1):
                if hit[0] in query_relevant_docs:
                    rr = 1.0 / rank
                    break
            mrr[f"MRR@{k}"].append(rr)

    for k in k_values:
        mrr[f"MRR@{k}"] = round(sum(mrr[f"MRR@{k}"]) / len(qrels), 5)
    return mrr


# Modified from https://github.com/embeddings-benchmark/mteb/blob/18f730696451a5aaa026494cecf288fd5cde9fd0/mteb/evaluation/evaluators/RetrievalEvaluator.py#L501
def evaluate_metrics(
    qrels: Dict[str, Dict[str, int]],
    results: Dict[str, Dict[str, float]],
    k_values: List[int],
) -> Tuple[
    Dict[str, float],
    Dict[str, float],
    Dict[str, float],
    Dict[str, float],
]:
    all_ndcgs, all_aps, all_recalls, all_precisions = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)

    map_string = "map_cut." + ",".join([str(k) for k in k_values])
    ndcg_string = "ndcg_cut." + ",".join([str(k) for k in k_values])
    recall_string = "recall." + ",".join([str(k) for k in k_values])
    precision_string = "P." + ",".join([str(k) for k in k_values])
    evaluator = pytrec_eval.RelevanceEvaluator(
        qrels, {map_string, ndcg_string, recall_string, precision_string}
    )
    scores = evaluator.evaluate(results)

    for query_id in scores.keys():
        for k in k_values:
            all_ndcgs[f"NDCG@{k}"].append(scores[query_id]["ndcg_cut_" + str(k)])
            all_aps[f"MAP@{k}"].append(scores[query_id]["map_cut_" + str(k)])
            all_recalls[f"Recall@{k}"].append(scores[query_id]["recall_" + str(k)])
            all_precisions[f"P@{k}"].append(scores[query_id]["P_" + str(k)])

    ndcg, _map, recall, precision = (
        all_ndcgs.copy(),
        all_aps.copy(),
        all_recalls.copy(),
        all_precisions.copy(),
    )

    for k in k_values:
        ndcg[f"NDCG@{k}"] = round(sum(ndcg[f"NDCG@{k}"]) / len(scores), 5)
        _map[f"MAP@{k}"] = round(sum(_map[f"MAP@{k}"]) / len(scores), 5)
        recall[f"Recall@{k}"] = round(sum(recall[f"Recall@{k}"]) / len(scores), 5)
        precision[f"P@{k}"] = round(sum(precision[f"P@{k}"]) / len(scores), 5)

    return ndcg, _map, recall, precision

def index(
    index_factory: str = "Flat", 
    corpus_embeddings: np.array = None, 
    load_path: str = None
):
    if corpus_embeddings is None:
        corpus_embeddings = np.load(load_path)
    print(corpus_embeddings.shape)
    # create faiss index
    print('create faiss index...')
    faiss_index = faiss.index_factory(corpus_embeddings.shape[1], index_factory, faiss.METRIC_INNER_PRODUCT)
    co = faiss.GpuMultipleClonerOptions()
    co.shard = True
    faiss_index = faiss.index_cpu_to_all_gpus(faiss_index, co)

    print('add data...')
    corpus_embeddings = corpus_embeddings.astype(np.float32)
    faiss_index.train(corpus_embeddings)
    faiss_index.add(corpus_embeddings)
    print('add over...')
    
    return faiss_index


def search(
    faiss_index: faiss.Index, 
    k: int = 100, 
    query_embeddings: np.array = None,
    load_path: str = None
):
    """
    1. Encode queries into dense embeddings;
    2. Search through faiss index
    """
    if query_embeddings is None:
        query_embeddings = np.load(load_path)

    query_size = len(query_embeddings)

    all_scores = []
    all_indices = []

    for i in tqdm(range(0, query_size, 32), desc="Searching"):
        j = min(i + 32, query_size)
        query_embedding = query_embeddings[i: j]
        score, indice = faiss_index.search(query_embedding.astype(np.float32), k=k)
        all_scores.append(score)
        all_indices.append(indice)

    all_scores = np.concatenate(all_scores, axis=0)
    all_indices = np.concatenate(all_indices, axis=0)
    return all_scores, all_indices