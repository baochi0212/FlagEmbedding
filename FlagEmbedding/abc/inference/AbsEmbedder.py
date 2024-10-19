import math
import torch
import queue
import logging
import numpy as np
from tqdm import tqdm, trange
import multiprocessing as mp
from multiprocessing import Queue
from abc import ABC, abstractmethod
from transformers import is_torch_npu_available
from typing import Any, Union, List, Dict, Literal


logger = logging.getLogger(__name__)


class AbsEmbedder(ABC):
    """
    Base class for embedder.
    Extend this class and implement `encode_queries`, `encode_passages`, `encode` for custom embedders.
    """
    def __init__(
        self,
        model_name_or_path: str,
        normalize_embeddings: bool = False,
        use_fp16: bool = False,
        query_instruction_for_retrieval: str = None,
        query_instruction_format: str = "{}{}", # specify the format of query_instruction_for_retrieval
        devices: Union[str, List[str]] = None,
        **kwargs: Any,
    ):
        self.model_name_or_path = model_name_or_path
        self.normalize_embeddings = normalize_embeddings
        self.use_fp16 = use_fp16
        self.query_instruction_for_retrieval = query_instruction_for_retrieval
        self.query_instruction_format = query_instruction_format
        self.target_devices = self.get_target_devices(devices)
        self.kwargs = kwargs
        
        # tokenizer and model are initialized in the child class
        self.tokenizer = None
        self.model = None
    
    @staticmethod
    def get_target_devices(devices: Union[str, List[str]]):
        if devices is None:
            if torch.cuda.is_available():
                return [f"cuda:{i}" for i in range(torch.cuda.device_count())]
            elif is_torch_npu_available():
                return [f"npu:{i}" for i in range(torch.npu.device_count())]
            elif torch.backends.mps.is_available():
                return [f"mps:{i}" for i in range(torch.mps.device_count())]
            else:
                return ["cpu"]
        elif isinstance(devices, str):
            return [devices]
        elif isinstance(devices, list):
            return devices
        else:
            raise ValueError("devices should be a string or a list of strings.")
    
    @staticmethod
    def get_detailed_instruct(instruction_format: str, instruction: str, query: str):
        return instruction_format.format(instruction, query)
    
    def encode_queries(
        self,
        queries: Union[List[str], str],
        batch_size: int = 256,
        max_length: int = 512,
        **kwargs: Any
    ):
        if self.query_instruction_for_retrieval is not None:
            if isinstance(queries, str):
                input_texts = self.get_detailed_instruct(self.query_instruction_format, self.query_instruction_for_retrieval, queries)
            else:
                input_texts = [self.get_detailed_instruct(self.query_instruction_format, self.query_instruction_for_retrieval, query) for query in queries]
        else:
            input_texts = queries
        
        return self.encode(
            input_texts,
            batch_size=batch_size,
            max_length=max_length,
            **kwargs
        )
    
    def encode_corpus(
        self,
        corpus: Union[List[str], str],
        batch_size: int = 256,
        max_length: int = 512,
        **kwargs: Any
    ):
        passage_instruction_for_retrieval = self.kwargs.get("passage_instruction_for_retrieval", None)
        passage_instruction_format = self.kwargs.get("passage_instruction_format", "{}{}")
        if passage_instruction_for_retrieval is not None:
            if isinstance(corpus, str):
                input_texts = self.get_detailed_instruct(passage_instruction_format, passage_instruction_for_retrieval, corpus)
            else:
                input_texts = [self.get_detailed_instruct(passage_instruction_format, passage_instruction_for_retrieval, passage) for passage in corpus]
        else:
            input_texts = corpus
        
        return self.encode(
            input_texts,
            batch_size=batch_size,
            max_length=max_length,
            **kwargs
        )
    
    def encode(
        self,
        sentences: Union[List[str], str],
        batch_size: int = 256,
        max_length: int = 512,
        **kwargs: Any
    ):
        if len(self.target_devices) == 1:
            return self.encode_single_device(
                sentences,
                batch_size=batch_size,
                max_length=max_length,
                device=self.target_devices[0],
                **kwargs
            )
        
        pool = self.start_multi_process_pool()
        embeddings = self.encode_multi_process(
            sentences,
            pool,
            batch_size=batch_size,
            max_length=max_length,
            **kwargs
        )
        self.stop_multi_process_pool(pool)
        return embeddings
    
    @abstractmethod
    def encode_single_device(
        self,
        sentences: Union[List[str], str],
        batch_size: int = 256,
        max_length: int = 512,
        device: str = None,
        **kwargs: Any,
    ):
        """
        This method should encode sentences and return embeddings on a single device.
        """
        pass

    # adapted from https://github.com/UKPLab/sentence-transformers/blob/1802076d4eae42ff0a5629e1b04e75785d4e193b/sentence_transformers/SentenceTransformer.py#L807
    def start_multi_process_pool(self) -> Dict[Literal["input", "output", "processes"], Any]:
        """
        Starts a multi-process pool to process the encoding with several independent processes
        via :meth:`SentenceTransformer.encode_multi_process <sentence_transformers.SentenceTransformer.encode_multi_process>`.

        This method is recommended if you want to encode on multiple GPUs or CPUs. It is advised
        to start only one process per GPU. This method works together with encode_multi_process
        and stop_multi_process_pool.

        Returns:
            Dict[str, Any]: A dictionary with the target processes, an input queue, and an output queue.
        """
        if self.model is None:
            raise ValueError("Model is not initialized.")

        logger.info("Start multi-process pool on devices: {}".format(", ".join(map(str, self.target_devices))))

        self.model.to("cpu")
        self.model.share_memory()
        ctx = mp.get_context("spawn")
        input_queue = ctx.Queue()
        output_queue = ctx.Queue()
        processes = []

        for device_id in tqdm(self.target_devices, desc='initial target device'):
            p = ctx.Process(
                target=AbsEmbedder._encode_multi_process_worker,
                args=(device_id, self, input_queue, output_queue),
                daemon=True,
            )
            p.start()
            processes.append(p)

        return {"input": input_queue, "output": output_queue, "processes": processes}
    
    # adapted from https://github.com/UKPLab/sentence-transformers/blob/1802076d4eae42ff0a5629e1b04e75785d4e193b/sentence_transformers/SentenceTransformer.py#L976
    @staticmethod
    def _encode_multi_process_worker(
        target_device: str, model: 'AbsEmbedder', input_queue: Queue, results_queue: Queue
    ) -> None:
        """
        Internal working process to encode sentences in multi-process setup
        """
        while True:
            try:
                chunk_id, sentences, kwargs = (
                    input_queue.get()
                )
                embeddings = model.encode_single_device(
                    sentences,
                    device=target_device,
                    **kwargs
                )

                results_queue.put([chunk_id, embeddings])
            except queue.Empty:
                break
    
    # copied from https://github.com/UKPLab/sentence-transformers/blob/1802076d4eae42ff0a5629e1b04e75785d4e193b/sentence_transformers/SentenceTransformer.py#L857
    @staticmethod
    def stop_multi_process_pool(pool: Dict[Literal["input", "output", "processes"], Any]) -> None:
        """
        Stops all processes started with start_multi_process_pool.

        Args:
            pool (Dict[str, object]): A dictionary containing the input queue, output queue, and process list.

        Returns:
            None
        """
        for p in pool["processes"]:
            p.terminate()

        for p in pool["processes"]:
            p.join()
            p.close()

        pool["input"].close()
        pool["output"].close()
    
    # adapted from https://github.com/UKPLab/sentence-transformers/blob/1802076d4eae42ff0a5629e1b04e75785d4e193b/sentence_transformers/SentenceTransformer.py#L877
    def encode_multi_process(
        self,
        sentences: List[str],
        pool: Dict[Literal["input", "output", "processes"], Any],
        **kwargs
    ):
        chunk_size = math.ceil(len(sentences) / len(pool["processes"]))

        input_queue = pool["input"]
        last_chunk_id = 0
        chunk = []

        for sentence in sentences:
            chunk.append(sentence)
            if len(chunk) >= chunk_size:
                input_queue.put(
                    [last_chunk_id, chunk, kwargs]
                )
                last_chunk_id += 1
                chunk = []

        if len(chunk) > 0:
            input_queue.put([last_chunk_id, chunk, kwargs])
            last_chunk_id += 1

        output_queue = pool["output"]
        results_list = sorted(
            [output_queue.get() for _ in trange(last_chunk_id, desc="Chunks")],
            key=lambda x: x[0],
        )
        embeddings = self._concatenate_results_from_multi_process([result[1] for result in results_list])
        return embeddings
    
    def _concatenate_results_from_multi_process(self, results_list: List[Union[torch.Tensor, np.ndarray, Any]]):
        if isinstance(results_list[0], torch.Tensor):
            return torch.cat(results_list, dim=0)
        elif isinstance(results_list[0], np.ndarray):
            return np.concatenate(results_list, axis=0)
        else:
            raise NotImplementedError("Unsupported type for results_list")
