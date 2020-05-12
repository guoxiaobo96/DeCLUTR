#!/usr/bin/env python
from __future__ import absolute_import, division, unicode_literals

import io
import json
import logging
import os
import sys
from pathlib import Path
from statistics import mean
from typing import Callable, List

import numpy as np
import torch
import typer

# TODO (John): I only need this for the sanitize method. Would be better if this was NOT a dependency for people
# who don't want to evaluate AllenNLP models.
from allennlp.common import util as common_util

app = typer.Typer()

# Set up logger
logger = logging.getLogger(__name__)

# URL to the TF Hub download for Google USE base model
GOOGLE_USE_TF_HUB = "https://tfhub.dev/google/universal-sentence-encoder/4"

DOWNSTREAM_TASKS = [
    "CR",
    "MR",
    "MPQA",
    "SUBJ",
    "SST2",
    "SST5",
    "TREC",
    "MRPC",
    "SNLI",
    "SICKEntailment",
    "SICKRelatedness",
    "STSBenchmark",
    "ImageCaptionRetrieval",
    "STS12",
    "STS13",
    "STS14",
    "STS15",
    "STS16",
]
PROBING_TASKS = [
    "Length",
    "WordContent",
    "Depth",
    "TopConstituents",
    "BigramShift",
    "Tense",
    "SubjNumber",
    "ObjNumber",
    "OddManOut",
    "CoordinationInversion",
]
TRANSFER_TASKS = DOWNSTREAM_TASKS + PROBING_TASKS

# Emoji's used in typer.secho calls
# See: https://github.com/carpedm20/emoji/blob/master/emoji/unicode_codes.py
WARNING = "\U000026A0"
SUCCESS = "\U00002705"
RUNNING = "\U000023F3"
SAVING = "\U0001F4BE"
FAST = "\U0001F3C3"
SCORE = "\U0001F4CB"


def _get_device(cuda_device):
    """Return a `torch.cuda` device if `torch.cuda.is_available()` and `cuda_device>=0`.
    Otherwise returns a `torch.cpu` device.
    """
    if cuda_device != -1 and torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")
    return device


def _compute_aggregate_scores(results):
    """Computes aggregate scores for the dev and test sets in a given SentEval `results`.
    """
    aggregate_scores = {
        "downstream": {"dev": 0, "test": 0},
        "probing": {"dev": 0, "test": 0},
        "all": {},
    }
    for task, scores in results.items():
        # Tasks belong to two groups only, "downstream" or "probing"
        task_set = "downstream" if task in DOWNSTREAM_TASKS else "probing"
        # All of this conditional logic is required to deal with the various ways performance is
        # reported for each task.
        # These two tasks report pearsonr for dev and spearman for test. Not sure why?
        if task == "SICKRelatedness" or task == "STSBenchmark":
            aggregate_scores[task_set]["dev"] += scores["devpearson"] * 100
            aggregate_scores[task_set]["test"] += scores["spearman"] * 100
        # There are no partitions for these tasks as no model is trained on top of the embeddings,
        # therefore we count their scores in both the dev and test accumulators.
        elif task.startswith("STS"):
            sts_score = scores["all"]["spearman"]["mean"] * 100
            aggregate_scores[task_set]["dev"] += sts_score
            aggregate_scores[task_set]["test"] += sts_score
        elif task == "ImageCaptionRetrieval":
            aggregate_scores[task_set]["dev"] += scores["devacc"]
            # Produce an average the same way SentEval produces its devacc average:
            # https://tinyurl.com/y9wcxjtr
            aggregate_scores[task_set]["test"] += mean(
                scores["acc"][0][:-1] + scores["acc"][-1][:-1]
            )
        # The rest of the tasks seem to all contain a dev and test accuracy
        elif "devacc" in scores and "acc" in scores:
            aggregate_scores[task_set]["dev"] += scores["devacc"]
            # f1 is in the score, average it with acc before accumulating
            if "f1" in scores:
                aggregate_scores[task_set]["test"] += mean([scores["acc"], scores["f1"]])
            else:
                aggregate_scores[task_set]["test"] += scores["acc"]
        else:
            raise ValueError(f'Found an unexpected field in results, "{task}".')

    # Aggregate scores for "downstream" tasks
    aggregate_scores["downstream"]["dev"] /= len(DOWNSTREAM_TASKS)
    aggregate_scores["downstream"]["test"] /= len(DOWNSTREAM_TASKS)
    # Aggregate scores for "probing" tasks
    aggregate_scores["probing"]["dev"] /= len(PROBING_TASKS)
    aggregate_scores["probing"]["test"] /= len(PROBING_TASKS)
    # Aggregate score across all of SentEval
    aggregate_scores["all"]["dev"] = mean(
        [aggregate_scores["downstream"]["dev"], aggregate_scores["probing"]["dev"]]
    )
    aggregate_scores["all"]["test"] = mean(
        [aggregate_scores["downstream"]["test"], aggregate_scores["probing"]["test"]]
    )

    return aggregate_scores


def _pad_sequences(sequences, pad_token):
    """Pads the elements of `sequences` with `pad_token` up to the longest sequence length."""
    max_len = len(max(sequences, key=len))
    padded_sequences = [seq + ([pad_token] * (max_len - len(seq))) for seq in sequences]
    return padded_sequences


def _setup_senteval(
    path_to_senteval: str, prototyping_config: bool = False, verbose: bool = False
) -> None:
    if verbose:
        logging.basicConfig(format="%(asctime)s : %(message)s", level=logging.DEBUG)

    # Set params for SentEval
    # See: https://github.com/facebookresearch/SentEval#senteval-parameters for explanation of the protoype config
    path_to_data = os.path.join(path_to_senteval, "data")
    if prototyping_config:
        typer.secho(
            (
                f"{WARNING} Using prototyping config. Pass --no-prototyping-config to get results comparable to"
                " the literature."
            ),
            fg=typer.colors.YELLOW,
            bold=True,
        )
        params_senteval = {"task_path": path_to_data, "usepytorch": True, "kfold": 5}
        params_senteval["classifier"] = {
            "nhid": 0,
            "optim": "rmsprop",
            "batch_size": 128,
            "tenacity": 3,
            "epoch_size": 2,
        }
    else:
        params_senteval = {"task_path": path_to_data, "usepytorch": True, "kfold": 10}
        params_senteval["classifier"] = {
            "nhid": 0,
            "optim": "adam",
            "batch_size": 64,
            "tenacity": 5,
            "epoch_size": 4,
        }

    return params_senteval


def _run_senteval(
    params, path_to_senteval: str, batcher: Callable, prepare: Callable, output_filepath: str = None
) -> None:
    sys.path.insert(0, path_to_senteval)
    import senteval

    typer.secho(
        f"{SUCCESS} SentEval repository and transfer task data loaded successfully.",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.secho(
        f"{RUNNING} Running evaluation. This may take a while!", fg=typer.colors.WHITE, bold=True
    )

    se = senteval.engine.SE(params, batcher, prepare)
    results = se.eval(TRANSFER_TASKS)
    typer.secho(f"{SUCCESS} Evaluation complete!", fg=typer.colors.GREEN, bold=True)

    aggregate_scores = _compute_aggregate_scores(results)
    typer.secho(
        f'{SCORE} Aggregate dev score: {aggregate_scores["all"]["dev"]:.2f}%',
        fg=typer.colors.WHITE,
        bold=True,
    )

    if output_filepath is not None:
        # Create the directory path if it doesn't exist
        output_filepath = Path(output_filepath)
        output_filepath.parents[0].mkdir(parents=True, exist_ok=True)
        with open(output_filepath, "w") as fp:
            # Convert anything that can't be serialized to JSON to a python type
            json_safe_results = common_util.sanitize(results)
            # Add aggregate scores to results dict
            json_safe_results["aggregate_scores"] = aggregate_scores
            json.dump(json_safe_results, fp, indent=2)
        typer.secho(
            f"{SAVING} Results saved to: {output_filepath}", fg=typer.colors.WHITE, bold=True
        )
    else:
        typer.secho(
            f"{WARNING} --output_filepath was not provided, printing results to console instead.",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        print(results)
    return


@app.command()
def random(
    path_to_senteval: str,
    embedding_dim: int = 512,
    output_filepath: str = None,
    prototyping_config: bool = False,
    verbose: bool = False,
) -> None:
    """Sanity check that evaluates randomly initialized vectors against the SentEval benchmark.
    """

    # SentEval prepare and batcher
    def prepare(params, samples):
        return

    @torch.no_grad()
    def batcher(params, batch):
        embeddings = np.random.rand(len(batch), embedding_dim)
        return embeddings

    # Performs a few setup steps and returns the SentEval params
    params_senteval = _setup_senteval(path_to_senteval, prototyping_config, verbose)
    _run_senteval(params_senteval, path_to_senteval, batcher, prepare, output_filepath)

    return


@app.command()
def bow(
    path_to_senteval: str,
    path_to_vectors: str,
    output_filepath: str = None,
    prototyping_config: bool = False,
    verbose: bool = False,
) -> None:
    """Evaluates pre-trained word vectors against the SentEval benchmark.
    Adapted from: https://github.com/facebookresearch/SentEval/blob/master/examples/bow.py.
    """
    # Create dictionary
    def create_dictionary(sentences, threshold=0):
        words = {}
        for s in sentences:
            for word in s:
                words[word] = words.get(word, 0) + 1

        if threshold > 0:
            newwords = {}
            for word in words:
                if words[word] >= threshold:
                    newwords[word] = words[word]
            words = newwords
        words["<s>"] = 1e9 + 4
        words["</s>"] = 1e9 + 3
        words["<p>"] = 1e9 + 2

        sorted_words = sorted(words.items(), key=lambda x: -x[1])  # inverse sort
        id2word = []
        word2id = {}
        for i, (w, _) in enumerate(sorted_words):
            id2word.append(w)
            word2id[w] = i

        return id2word, word2id

    # Get word vectors from vocabulary (glove, word2vec, fasttext ..)
    def get_wordvec(path_to_vec, word2id, skip_header=False):
        word_vec = {}

        with io.open(path_to_vec, "r", encoding="utf-8") as f:
            if skip_header:
                next(f)
            for line in f:
                word, vec = line.split(" ", 1)
                if word in word2id:
                    word_vec[word] = np.fromstring(vec, sep=" ")

        logging.info(
            "Found {0} words with word vectors, out of \
            {1} words".format(
                len(word_vec), len(word2id)
            )
        )
        return word_vec

    # SentEval prepare and batcher
    def prepare(params, samples):
        _, params.word2id = create_dictionary(samples)
        params.word_vec = get_wordvec(params.path_to_vectors, params.word2id, params.skip_header)
        # TODO (John): This was hardcoded in SentEval example script. Can we set it based on the loaded vectors?
        params.wvec_dim = 300
        return

    def batcher(params, batch):
        batch = [sent if sent != [] else ["."] for sent in batch]
        embeddings = []

        for sent in batch:
            sentvec = []
            for word in sent:
                if word in params.word_vec:
                    sentvec.append(params.word_vec[word])
            if not sentvec:
                vec = np.zeros(params.wvec_dim)
                sentvec.append(vec)
            sentvec = np.mean(sentvec, 0)
            embeddings.append(sentvec)

        embeddings = np.vstack(embeddings)
        return embeddings

    # A dumb heuristic to determine whether or not the file has a header that we should skip.
    skip_header = True if "word2vec" or "fasttext" in path_to_vectors.lower() else False

    # Performs a few setup steps and returns the SentEval params
    params_senteval = _setup_senteval(path_to_senteval, prototyping_config, verbose)
    params_senteval["path_to_vectors"] = path_to_vectors
    params_senteval["skip_header"] = skip_header
    _run_senteval(params_senteval, path_to_senteval, batcher, prepare, output_filepath)

    return


@app.command()
def infersent(
    path_to_senteval: str,
    output_filepath: str = None,
    cuda_device: int = -1,
    prototyping_config: bool = False,
    verbose: bool = False,
) -> None:
    """Evaluates an InferSent model against the SentEval benchmark
    (see: https://github.com/facebookresearch/InferSent for information on the pre-trained model).
    Adapted from: https://github.com/facebookresearch/SentEval/blob/master/examples/infersent.py.
    """
    from models import InferSent

    def prepare(params, samples):
        params.infersent.build_vocab([" ".join(s) for s in samples], tokenize=False)

    def batcher(params, batch):
        sentences = [" ".join(s) for s in batch]
        embeddings = params.infersent.encode(sentences, bsize=params.batch_size, tokenize=False)
        return embeddings

    # Determine the torch device
    device = _get_device(cuda_device)

    # Load InferSent model
    V = 2
    MODEL_PATH = "encoder/infersent%s.pkl" % V
    params_model = {
        "bsize": 64,
        "word_emb_dim": 300,
        "enc_lstm_dim": 2048,
        "pool_type": "max",
        "dpout_model": 0.0,
        "version": V,
    }
    infersent = InferSent(params_model)
    infersent.load_state_dict(torch.load(MODEL_PATH))
    infersent.to(device)
    # Load and initialize the model with word vectors
    W2V_PATH = "fastText/crawl-300d-2M.vec"
    infersent.set_w2v_path(W2V_PATH)

    # Performs a few setup steps and returns the SentEval params
    params_senteval = _setup_senteval(path_to_senteval, prototyping_config, verbose)
    params_senteval["infersent"] = infersent
    _run_senteval(params_senteval, path_to_senteval, batcher, prepare, output_filepath)

    return


@app.command()
def google_use(
    path_to_senteval: str,
    output_filepath: str = None,
    tfhub_model_url: str = GOOGLE_USE_TF_HUB,
    tfhub_cache_dir: str = None,
    prototyping_config: bool = False,
    verbose: bool = False,
) -> None:
    """Evaluates a Google USE model from TF Hub against the SentEval benchmark
    (see: https://tfhub.dev/google/universal-sentence-encoder-large/5 for information on the pre-trained model).
    If you would like to use a cached model instead of downloading one, following the instructions
    here: https://medium.com/@xianbao.qian/how-to-run-tf-hub-locally-without-internet-connection-4506b850a915
    and provide the TF Hub cache dir with `--tfhub-cache-dir`.
    """
    if tfhub_cache_dir is not None:
        os.environ["TFHUB_CACHE_DIR"] = tfhub_cache_dir

    # This prevents import errors when a user doesn't have the dependencies for this command installed
    import tensorflow_hub as hub

    # SentEval prepare and batcher
    def prepare(params, samples):
        return

    def batcher(params, batch):
        batch = [" ".join(sent) if sent != [] else "." for sent in batch]
        embeddings = params["google_use"](batch).numpy()
        return embeddings

    # Download the Google Universal Sentence Encoder (will be cached)
    encoder = hub.load(tfhub_model_url)
    typer.secho(
        f"{SUCCESS} Google USE model from TensorFlow Hub loaded successfully.",
        fg=typer.colors.GREEN,
        bold=True,
    )

    # Performs a few setup steps and returns the SentEval params
    params_senteval = _setup_senteval(path_to_senteval, prototyping_config, verbose)
    params_senteval["google_use"] = encoder
    _run_senteval(params_senteval, path_to_senteval, batcher, prepare, output_filepath)

    return


@app.command()
def transformers(
    path_to_senteval: str,
    pretrained_model_name_or_path: str,
    output_filepath: str = None,
    mean_pool: bool = False,
    cuda_device: int = -1,
    opt_level: str = None,
    prototyping_config: bool = False,
    verbose: bool = False,
) -> None:
    """Evaluates a pre-trained model from the Transformers library against the SentEval benchmark.
    """

    # This prevents import errors when a user doesn't have the dependencies for this command installed
    from transformers import AutoModel, AutoTokenizer

    # SentEval prepare and batcher
    def prepare(params, samples):
        return

    @torch.no_grad()
    def batcher(params, batch):
        # Some SentEval tasks contain empty batches which triggers an error with HuggingfFace's tokenizer.
        # I am using the solution found in the SentEvel repo here:
        # https://github.com/facebookresearch/SentEval/blob/6b13ac2060332842f59e84183197402f11451c94/examples/bow.py#L77
        batch = [sent if sent != [] else ["."] for sent in batch]
        # Re-tokenize the input text using the pre-trained tokenizer
        batch = [tokenizer.encode(" ".join(tokens)) for tokens in batch]
        batch = _pad_sequences(batch, tokenizer.pad_token_id)

        # To embed with transformers, we (minimally) need the input ids and the attention masks.
        input_ids = torch.as_tensor(batch, device=params.device)
        attention_masks = torch.where(
            input_ids == params.tokenizer.pad_token_id,
            torch.zeros_like(input_ids),
            torch.ones_like(input_ids),
        )

        sequence_output, _ = params.model(input_ids=input_ids, attention_mask=attention_masks)

        # If mean_pool, we take the average of the token-level embeddings, accounting for pads.
        # Otherwise, we take the pooled output for this specific model, which is typically the linear projection
        # of a special tokens embedding, like [CLS] or <s>, which is prepended to the input during tokenization.
        if mean_pool:
            embeddings = torch.sum(
                sequence_output * attention_masks.unsqueeze(-1), dim=1
            ) / torch.clamp(torch.sum(attention_masks, dim=1, keepdims=True), min=1e-9)
        else:
            # TODO (John): Replace this with the built in pooler from the Transformers lib,
            # as it will check if the last token should be used.
            embeddings = sequence_output[:, 0, :]

        embeddings = embeddings.cpu().numpy()
        return embeddings

    # Determine the torch device
    device = _get_device(cuda_device)

    # Load the Transformers tokenizer
    tokenizer = AutoTokenizer.from_pretrained(pretrained_model_name_or_path)
    typer.secho(
        f'{SUCCESS} Tokenizer "{pretrained_model_name_or_path}" from Transformers loaded successfully.',
        fg=typer.colors.GREEN,
        bold=True,
    )

    # Load the Transformers model
    model = AutoModel.from_pretrained(pretrained_model_name_or_path).to(device)
    model.eval()
    typer.secho(
        f'{SUCCESS} Model "{pretrained_model_name_or_path}" from Transformers loaded successfully.',
        fg=typer.colors.GREEN,
        bold=True,
    )

    # Performs a few setup steps and returns the SentEval params
    params_senteval = _setup_senteval(path_to_senteval, prototyping_config, verbose)
    params_senteval["tokenizer"] = tokenizer
    params_senteval["model"] = model
    params_senteval["device"] = device
    _run_senteval(params_senteval, path_to_senteval, batcher, prepare, output_filepath)

    return


@app.command()
def sentence_transformers(
    path_to_senteval: str,
    pretrained_model_name_or_path: str,
    output_filepath: str = None,
    cuda_device: int = -1,
    opt_level: str = None,
    prototyping_config: bool = False,
    verbose: bool = False,
) -> None:
    """Evaluates a pre-trained model from the Sentence Transformers library against the SentEval benchmark.
    """

    # This prevents import errors when a user doesn't have the dependencies for this command installed
    from sentence_transformers import SentenceTransformer

    # SentEval prepare and batcher
    def prepare(params, samples):
        return

    @torch.no_grad()
    def batcher(params, batch):
        # Some SentEval tasks contain empty batches which triggers an error with HuggingfFace's tokenizer.
        # I am using the solution found in the SentEvel repo here:
        # https://github.com/facebookresearch/SentEval/blob/6b13ac2060332842f59e84183197402f11451c94/examples/bow.py#L77
        batch = [sent if sent != [] else ["."] for sent in batch]
        # Sentence Transformers API expects un-tokenized sentences.
        batch = [" ".join(tokens) for tokens in batch]
        embeddings = params.model.encode(batch, batch_size=len(batch), show_progress_bar=False)
        embeddings = np.vstack(embeddings)
        return embeddings

    # Determine the torch device
    device = _get_device(cuda_device)

    # Load the Sentence Transformers tokenizer
    model = SentenceTransformer(pretrained_model_name_or_path, device=device)
    model.eval()
    typer.secho(
        f'{SUCCESS} Model "{pretrained_model_name_or_path}" from Sentence Transformers loaded successfully.',
        fg=typer.colors.GREEN,
        bold=True,
    )

    # Performs a few setup steps and returns the SentEval params
    params_senteval = _setup_senteval(path_to_senteval, prototyping_config, verbose)
    params_senteval["model"] = model
    _run_senteval(params_senteval, path_to_senteval, batcher, prepare, output_filepath)

    return


@app.command()
def allennlp(
    path_to_senteval: str,
    path_to_allennlp_archive: str,
    output_filepath: str = None,
    weights_file: str = None,
    cuda_device: int = -1,
    opt_level: str = "O0",
    output_dict_field: str = "embeddings",
    predictor_name: str = None,
    include_package: List[str] = None,
    prototyping_config: bool = False,
    verbose: bool = False,
) -> None:
    """Evaluates a trained AllenNLP model against the SentEval benchmark.
    """

    # This prevents import errors when a user doesn't have the dependencies for this command installed
    from allennlp.models.archival import load_archive
    from allennlp.predictors import Predictor

    # SentEval prepare and batcher
    def prepare(params, samples):
        return

    @torch.no_grad()
    def batcher(params, batch):
        # Some SentEval tasks contain empty batches which triggers an error with HuggingfFace's tokenizer.
        # I am using the solution found in the SentEvel repo here:
        # https://github.com/facebookresearch/SentEval/blob/6b13ac2060332842f59e84183197402f11451c94/examples/bow.py#L77
        batch = [sent if sent != [] else ["."] for sent in batch]
        # HACK (John): This is neccesary b/c at some point SentEval converts these to bytes.
        # We probably want a better fix.
        batch = [
            [token.decode("utf-8") if isinstance(token, bytes) else token for token in sent]
            for sent in batch
        ]
        # Re-tokenize the input text using the tokenizer of the dataset reader
        inputs = [{"text": " ".join(tokens)} for tokens in batch]
        outputs = params.predictor.predict_batch_json(inputs)
        # AllenNLP models return a dictionary, so we need to access the embeddings with the given key.
        embeddings = [output[output_dict_field] for output in outputs]

        embeddings = np.vstack(embeddings)
        return embeddings

    # This allows us to import custom dataset readers and models that may exist in the AllenNLP archive.
    # See: https://tinyurl.com/whkmoqh
    include_package = include_package or []
    for package_name in include_package:
        common_util.import_module_and_submodules(package_name)

    # Load the archived Model
    archive = load_archive(
        path_to_allennlp_archive,
        cuda_device=cuda_device,
        opt_level=opt_level,
        weights_file=weights_file,
    )
    predictor = Predictor.from_archive(archive, predictor_name)
    typer.secho(
        f'{SUCCESS} Model from AllenNLP archive "{path_to_allennlp_archive}" loaded successfully.',
        fg=typer.colors.GREEN,
        bold=True,
    )

    # Performs a few setup steps and returns the SentEval params
    params_senteval = _setup_senteval(path_to_senteval, prototyping_config, verbose)
    params_senteval["predictor"] = predictor
    _run_senteval(params_senteval, path_to_senteval, batcher, prepare, output_filepath)

    return


if __name__ == "__main__":
    app()
